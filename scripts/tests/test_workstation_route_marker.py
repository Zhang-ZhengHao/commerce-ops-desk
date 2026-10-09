"""Contracts for binding the Caddy smoke probe to the installed route."""

from __future__ import annotations

import importlib.util
import sys
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
ROUTE_REVISION_HEADER = "X-CommerceOps-Route-Revision"
READY_BODY = b'{"status":"ready","database":"reachable"}'


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_route_marker", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def route_state(module: ModuleType, *, revision: str | None) -> object:
    profile = (
        module.CADDY_PROFILE_HARDENED
        if revision is not None
        else module.CADDY_PROFILE_LEGACY_V020
    )
    upstream = module.UpstreamIdentity(
        schema=1,
        docker_daemon_id="local-daemon-id",
        container_id="1" * 64,
        container_name="app-commerce-ops-desk-candidate-0123456789ab",
        image_id="sha256:" + "2" * 64,
        image_reference="commerce-ops-desk:" + "3" * 40,
        source_sha="3" * 40,
        host_port=18_088,
        data_path=("/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab"),
        data_device=64_769,
        data_inode=55_451_027,
        network_name="commerce-ops-candidate-0123456789ab_default",
        network_id="4" * 64,
        network_endpoint_id="5" * 64,
        runtime_sha256="6" * 64,
    )
    return module.RouteState(
        fragment_sha256="7" * 64,
        profile=profile,
        route_revision=revision,
        upstream=upstream,
        deployment_assets=None,
    )


class FakeHeaders:
    """Expose raw repeated header fields through the HTTPMessage API."""

    def __init__(self, revision_values: list[str] | None) -> None:
        self.revision_values = revision_values
        self.get_all_calls: list[str] = []

    def get_all(self, name: str, failobj: Any = None) -> Any:
        self.get_all_calls.append(name)
        assert name.lower() == ROUTE_REVISION_HEADER.lower()
        if self.revision_values is None:
            return failobj
        return list(self.revision_values)


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = READY_BODY,
        revision_values: list[str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = FakeHeaders(revision_values)
        self.read_sizes: list[int] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[urllib.request.Request, int]] = []

    def open(self, request: urllib.request.Request, timeout: int) -> FakeResponse:
        self.calls.append((request, timeout))
        return self.response


def install_response(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse,
) -> FakeOpener:
    opener = FakeOpener(response)
    monkeypatch.setattr(
        module.urllib.request,
        "build_opener",
        lambda *_handlers: opener,
    )
    return opener


@pytest.mark.parametrize(
    "body",
    [
        READY_BODY,
        READY_BODY + b" " * (4_096 - len(READY_BODY)),
    ],
    ids=["normal-body", "exactly-4096-bytes"],
)
def test_hardened_smoke_accepts_one_matching_route_revision(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    module = load_deploy_tool()
    revision = "a" * 64
    response = FakeResponse(body=body, revision_values=[revision])
    opener = install_response(module, monkeypatch, response)

    module._smoke_caddy(route_state(module, revision=revision))

    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert timeout == 5
    assert request.full_url == "http://127.0.0.1/ready"
    assert request.get_header("Host") == module.CADDY_HOST
    assert request.get_header("Accept") == "application/json"
    assert response.read_sizes == [4_097]
    assert response.headers.get_all_calls == [ROUTE_REVISION_HEADER]


@pytest.mark.parametrize(
    "revision_values",
    [
        None,
        ["b" * 64],
        ["a" * 64, "a" * 64],
        ["forged-by-upstream", "a" * 64],
    ],
    ids=[
        "missing",
        "wrong",
        "duplicate-matching",
        "upstream-forged-duplicate",
    ],
)
def test_hardened_smoke_rejects_missing_wrong_or_repeated_route_revision(
    monkeypatch: pytest.MonkeyPatch,
    revision_values: list[str] | None,
) -> None:
    module = load_deploy_tool()
    expected_revision = "a" * 64
    response = FakeResponse(revision_values=revision_values)
    install_response(module, monkeypatch, response)

    with pytest.raises(module.DeploymentError):
        module._smoke_caddy(route_state(module, revision=expected_revision))

    assert response.headers.get_all_calls == [ROUTE_REVISION_HEADER]


def test_legacy_smoke_accepts_a_response_without_route_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    response = FakeResponse(revision_values=None)
    install_response(module, monkeypatch, response)

    module._smoke_caddy(route_state(module, revision=None))

    assert response.headers.get_all_calls == [ROUTE_REVISION_HEADER]


@pytest.mark.parametrize(
    "revision_values",
    [["a" * 64], [""], ["a" * 64, "b" * 64]],
    ids=["one-header", "empty-header", "repeated-headers"],
)
def test_legacy_smoke_rejects_any_route_revision_header(
    monkeypatch: pytest.MonkeyPatch,
    revision_values: list[str],
) -> None:
    module = load_deploy_tool()
    response = FakeResponse(revision_values=revision_values)
    install_response(module, monkeypatch, response)

    with pytest.raises(module.DeploymentError):
        module._smoke_caddy(route_state(module, revision=None))

    assert response.headers.get_all_calls == [ROUTE_REVISION_HEADER]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, READY_BODY),
        (200, b"x" * 4_097),
        (200, b"not-json"),
        (200, b'{"status":"ready","database":"unreachable"}'),
    ],
    ids=["wrong-status", "body-too-large", "non-json-body", "wrong-json-body"],
)
def test_smoke_keeps_status_body_and_size_contract(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    module = load_deploy_tool()
    revision = "a" * 64
    response = FakeResponse(
        status=status,
        body=body,
        revision_values=[revision],
    )
    install_response(module, monkeypatch, response)

    with pytest.raises(module.DeploymentError):
        module._smoke_caddy(route_state(module, revision=revision))

    assert response.read_sizes == [4_097]
