"""End-to-end transaction lifecycle coverage for stale rollback replay."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_lifecycle_replay",
        DEPLOY_TOOL,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_upstream(module: ModuleType, *, marker: str, port: int) -> object:
    endpoint_marker = "e" if marker != "e" else "f"
    runtime_marker = "f" if marker != "f" else "a"
    return module._validate_upstream_identity_payload(
        {
            "schema": module.UPSTREAM_IDENTITY_SCHEMA,
            "docker_daemon_id": f"daemon-{marker}",
            "container_id": marker * 64,
            "container_name": f"commerce-ops-{marker}",
            "image_id": "sha256:" + marker * 64,
            "image_reference": f"commerce-ops-desk:{marker * 40}",
            "source_sha": marker * 40,
            "host_port": port,
            "data_path": f"/srv/commerce-ops-desk/data-{marker}",
            "data_device": 64_769,
            "data_inode": 55_451_000 + port,
            "network_name": f"commerce-ops-{marker}_default",
            "network_id": marker * 64,
            "network_endpoint_id": endpoint_marker * 64,
            "runtime_sha256": runtime_marker * 64,
        }
    )


def deployment_assets(module: ModuleType, marker: str) -> object:
    return module._validate_deployment_assets(
        {
            "schema": module.DEPLOYMENT_ASSET_SCHEMA,
            "sha256": {path: marker * 64 for path in module.DEPLOYMENT_ASSET_PATHS},
        }
    )


@contextmanager
def transaction_lock(_runner: object) -> Iterator[None]:
    yield


def test_switch_then_rollback_rejects_t1_replay_before_any_fragment_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory = tmp_path / "root" / "transactions"
    active_path = transaction_directory / "active.json"
    state_directory = tmp_path / "state"
    site_path = tmp_path / "root" / "sites" / "commerce-ops-desk.conf"
    monkeypatch.setattr(module, "CADDY_TRANSACTION_DIRECTORY", transaction_directory)
    monkeypatch.setattr(module, "CADDY_ACTIVE_STATE", active_path)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "CADDY_SITE", site_path)

    legacy_upstream = make_upstream(module, marker="1", port=18_087)
    legacy_fragment = module.render_caddy_template(
        module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8"),
        legacy_upstream.host_port,
    )
    legacy_route = module.RouteState(
        fragment_sha256=hashlib.sha256(legacy_fragment.encode()).hexdigest(),
        profile=module.CADDY_PROFILE_LEGACY_V020,
        route_revision=None,
        upstream=legacy_upstream,
        deployment_assets=None,
    )

    assets = deployment_assets(module, "6")
    templates = MappingProxyType(
        {
            module.CADDY_PROFILE_HARDENED: module.CADDY_TEMPLATE.read_text(
                encoding="utf-8"
            ),
            module.CADDY_PROFILE_LEGACY_V020: (
                module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8")
            ),
        }
    )
    candidate_upstream = make_upstream(module, marker="2", port=18_088)
    verified_candidate = module.VerifiedCandidate(
        deployment_assets=module.VerifiedDeploymentAssets(
            identity=assets,
            compose_yaml="verified compose\n",
            caddy_templates=templates,
        ),
        upstream=candidate_upstream,
    )
    candidate_revision = module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        candidate_upstream,
        assets,
    )
    candidate_fragment = module.render_caddy_template(
        templates[module.CADDY_PROFILE_HARDENED],
        candidate_upstream.host_port,
        route_revision=candidate_revision,
    )
    candidate_state = {
        "source_sha": candidate_upstream.source_sha,
        "candidate_port": candidate_upstream.host_port,
    }

    root_files: dict[Path, str] = {}
    current_site = legacy_fragment
    fragment_reads: list[Path] = []
    site_installs: list[str] = []

    def publish_immutable(_runner: object, source: Path, target: Path) -> None:
        assert target not in root_files
        root_files[target] = source.read_text(encoding="utf-8")

    def read_transaction(_runner: object, path: Path) -> str:
        if path.suffix == ".conf":
            fragment_reads.append(path)
        return root_files[path]

    def root_file_exists(_runner: object, path: Path) -> bool:
        return path in root_files

    def commit_active(
        _runner: object,
        path: Path,
        text: str,
        *,
        expected_sha256: str | None,
    ) -> None:
        assert path == active_path
        previous = root_files.get(path)
        if expected_sha256 is None:
            assert previous is None
        else:
            assert previous is not None
            assert hashlib.sha256(previous.encode()).hexdigest() == expected_sha256
        root_files[path] = text

    def read_site(_runner: object) -> str:
        fragment_reads.append(site_path)
        return current_site

    def install_site(
        _runner: object,
        content: str,
        *,
        expected_sha256: str,
        expected_current_sha256: str,
    ) -> None:
        nonlocal current_site
        assert hashlib.sha256(content.encode()).hexdigest() == expected_sha256
        assert (
            hashlib.sha256(current_site.encode()).hexdigest() == expected_current_sha256
        )
        site_installs.append(content)
        current_site = content

    monkeypatch.setattr(module, "_caddy_transaction_lock", transaction_lock)
    monkeypatch.setattr(module, "_load_candidate_state", lambda _path: candidate_state)
    monkeypatch.setattr(
        module,
        "_assert_state_candidate_ready",
        lambda _runner, _state: verified_candidate,
    )
    monkeypatch.setattr(
        module,
        "_load_exact_legacy_bootstrap_route",
        lambda _runner, *, caddy_templates: legacy_route,
    )
    monkeypatch.setattr(module, "_ensure_caddy_transaction_directory", lambda _: None)
    monkeypatch.setattr(module, "_install_root_owned_immutable_file", publish_immutable)
    monkeypatch.setattr(module, "_read_root_owned_transaction_text", read_transaction)
    monkeypatch.setattr(module, "_root_owned_regular_file_exists", root_file_exists)
    monkeypatch.setattr(module, "_atomic_install_root_owned_text", commit_active)
    monkeypatch.setattr(module, "_read_current_site", read_site)
    monkeypatch.setattr(module, "_atomic_install_site", install_site)
    monkeypatch.setattr(
        module,
        "_adapt_fragment",
        lambda _runner, fragment: {"canonical_fragment": fragment},
    )
    monkeypatch.setattr(module, "_assert_upstream_ready", lambda *_: None)
    monkeypatch.setattr(module, "_validate_caddy", lambda _: None)
    monkeypatch.setattr(module, "_reload_caddy", lambda _: None)
    monkeypatch.setattr(module, "_smoke_caddy", lambda _: None)
    monkeypatch.setattr(module, "_assert_upstream_identity_current", lambda *_: None)
    monkeypatch.setattr(
        module,
        "_assert_active_caddy_route",
        lambda _runner, expected: (
            pytest.fail("active route/site diverged")
            if expected != current_site
            else None
        ),
    )

    runner = object()
    t1_backup = module.switch_candidate(
        argparse.Namespace(state=str(tmp_path / "candidate.json")),
        runner,
    )
    t1_head = module._load_active_caddy_chain(runner, allow_missing=False)
    assert t1_head is not None
    assert t1_head.transaction.operation == "switch"
    assert t1_head.transaction.backup_path == t1_backup
    assert t1_head.installed.route_revision == candidate_revision
    assert current_site == candidate_fragment

    trusted_t1 = module._load_validated_backup(runner, t1_backup)
    assert trusted_t1.parent.transaction_id == t1_head.transaction_id
    assert trusted_t1.fragment == legacy_fragment

    t2_backup = module.rollback_site(
        argparse.Namespace(backup=str(t1_backup)),
        runner,
    )
    t2_head = module._load_active_caddy_chain(runner, allow_missing=False)
    assert t2_head is not None
    assert t2_head.transaction.operation == "rollback"
    assert t2_head.transaction.backup_path == t2_backup
    assert t2_head.transaction_id != t1_head.transaction_id
    assert current_site == legacy_fragment
    t2_ledger = json.loads(t2_head.transaction.ledger_text)
    assert t2_ledger["parent"]["transaction_id"] == t1_head.transaction_id
    assert t2_head.installed.fragment_sha256 == legacy_route.fragment_sha256

    fragment_reads.clear()
    site_installs.clear()
    with pytest.raises(
        module.DeploymentError,
        match="rollback backup is not authorized by the current head",
    ):
        module.rollback_site(
            argparse.Namespace(backup=str(t1_backup)),
            runner,
        )

    assert fragment_reads == []
    assert site_installs == []
