"""Switch and rollback wiring contracts for the active Caddy chain."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PARENT_TRANSACTION_ID = "20261009T120000Z-11111111111111111111111111111111"
BOOTSTRAP_ID = "b" * 64


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_active_chain_integration",
        DEPLOY_TOOL,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_upstream(
    module: ModuleType,
    *,
    marker: str,
    port: int,
) -> object:
    source_sha = marker * 40
    endpoint_marker = "e" if marker != "e" else "f"
    runtime_marker = "f" if marker != "f" else "a"
    return module._validate_upstream_identity_payload(
        {
            "schema": module.UPSTREAM_IDENTITY_SCHEMA,
            "docker_daemon_id": f"daemon-{marker}",
            "container_id": marker * 64,
            "container_name": f"commerce-ops-{marker}",
            "image_id": "sha256:" + marker * 64,
            "image_reference": f"commerce-ops-desk:{source_sha}",
            "source_sha": source_sha,
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


def asset_identity(module: ModuleType, marker: str) -> dict[str, object]:
    return module._validate_deployment_assets(
        {
            "schema": module.DEPLOYMENT_ASSET_SCHEMA,
            "sha256": {path: marker * 64 for path in module.DEPLOYMENT_ASSET_PATHS},
        }
    )


def hardened_route(
    module: ModuleType,
    *,
    fragment: str,
    upstream: object,
    assets: Mapping[str, object],
) -> object:
    revision = module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        upstream,
        assets,
    )
    return module.RouteState(
        fragment_sha256=hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
        profile=module.CADDY_PROFILE_HARDENED,
        route_revision=revision,
        upstream=upstream,
        deployment_assets=assets,
    )


def legacy_route(
    module: ModuleType,
    *,
    fragment: str,
    upstream: object,
) -> object:
    return module.RouteState(
        fragment_sha256=hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
        profile=module.CADDY_PROFILE_LEGACY_V020,
        route_revision=None,
        upstream=upstream,
        deployment_assets=None,
    )


def parent_chain(
    module: ModuleType,
    tmp_path: Path,
    *,
    current: object,
    backup: object,
) -> object:
    ledger_text = '{"transaction":"parent"}\n'
    transaction = module.PersistedCaddyTransaction(
        transaction_id=PARENT_TRANSACTION_ID,
        operation="switch",
        bootstrap_id=BOOTSTRAP_ID,
        backup_path=tmp_path / "parent-backup.conf",
        ledger_path=tmp_path / "parent-ledger.json",
        ledger_text=ledger_text,
        ledger_sha256=hashlib.sha256(ledger_text.encode("utf-8")).hexdigest(),
        backup=backup,
        installed=current,
    )
    active_text = '{"transaction":"parent"}\n'
    return module.ActiveCaddyChain(
        active_text=active_text,
        active_sha256=hashlib.sha256(active_text.encode("utf-8")).hexdigest(),
        bootstrap_id=BOOTSTRAP_ID,
        transaction_id=PARENT_TRANSACTION_ID,
        installed=current,
        transaction=transaction,
    )


@contextmanager
def transaction_lock(_runner: object) -> Iterator[None]:
    yield


def candidate_fixture(module: ModuleType) -> tuple[dict[str, object], object, str]:
    assets = asset_identity(module, "6")
    hardened_template = module.CADDY_TEMPLATE.read_text(encoding="utf-8")
    templates = MappingProxyType(
        {
            module.CADDY_PROFILE_HARDENED: hardened_template,
            module.CADDY_PROFILE_LEGACY_V020: (
                module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8")
            ),
        }
    )
    verified_assets = module.VerifiedDeploymentAssets(
        identity=assets,
        compose_yaml="verified compose\n",
        caddy_templates=templates,
    )
    candidate_upstream = make_upstream(module, marker="2", port=18_088)
    verified_candidate = module.VerifiedCandidate(
        deployment_assets=verified_assets,
        upstream=candidate_upstream,
    )
    revision = module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        candidate_upstream,
        assets,
    )
    rendered = module.render_caddy_template(
        hardened_template,
        18_088,
        route_revision=revision,
    )
    state: dict[str, object] = {
        "source_sha": SOURCE_SHA,
        "candidate_port": 18_088,
    }
    return state, verified_candidate, rendered


def install_candidate_stubs(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: dict[str, object],
    verified_candidate: object,
) -> None:
    monkeypatch.setattr(module, "_caddy_transaction_lock", transaction_lock)
    monkeypatch.setattr(module, "_load_candidate_state", lambda _path: state)
    monkeypatch.setattr(
        module,
        "_assert_state_candidate_ready",
        lambda _runner, _state: verified_candidate,
    )


def assert_candidate_route(
    module: ModuleType,
    route: object,
    verified_candidate: object,
    rendered: str,
) -> None:
    assert isinstance(route, module.RouteState)
    assert route.fragment_sha256 == hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    assert route.profile == module.CADDY_PROFILE_HARDENED
    assert route.upstream is verified_candidate.upstream
    assert route.route_revision == module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        verified_candidate.upstream,
        verified_candidate.deployment_assets.identity,
    )
    assert dict(route.deployment_assets) == dict(
        verified_candidate.deployment_assets.identity
    )


def test_switch_with_a_head_uses_parent_installed_and_passes_a_complete_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state, verified_candidate, rendered = candidate_fixture(module)
    install_candidate_stubs(
        module,
        monkeypatch,
        state=state,
        verified_candidate=verified_candidate,
    )
    current_fragment = "current hardened fragment\n"
    current = hardened_route(
        module,
        fragment=current_fragment,
        upstream=make_upstream(module, marker="1", port=18_087),
        assets=asset_identity(module, "7"),
    )
    parent = parent_chain(
        module,
        tmp_path,
        current=current,
        backup=current,
    )
    events: list[str] = []

    def load_active(_runner: object, *, allow_missing: bool) -> object:
        assert allow_missing is True
        events.append("load-head")
        return parent

    def reject_bootstrap(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy bootstrap must not run when an active head exists")

    def install(
        _runner: object,
        fragment: str,
        *,
        label: str,
        operation: str,
        current_route: object,
        installed_route: object,
        parent: object | None,
        caddy_templates: Mapping[str, str] | None = None,
    ) -> Path:
        events.append("install")
        assert fragment == rendered
        assert label == f"before-{SOURCE_SHA[:12]}"
        assert operation == "switch"
        assert current_route is parent_chain_value.installed
        assert parent is parent_chain_value
        assert caddy_templates is verified_candidate.deployment_assets.caddy_templates
        assert_candidate_route(
            module,
            installed_route,
            verified_candidate,
            rendered,
        )
        return tmp_path / "switch-backup.conf"

    parent_chain_value = parent
    monkeypatch.setattr(module, "_load_active_caddy_chain", load_active)
    monkeypatch.setattr(
        module,
        "_load_exact_legacy_bootstrap_route",
        reject_bootstrap,
        raising=False,
    )
    monkeypatch.setattr(module, "_install_caddy_fragment", install)

    backup = module.switch_candidate(
        argparse.Namespace(state=str(tmp_path / "candidate.json")),
        object(),
    )

    assert backup == tmp_path / "switch-backup.conf"
    assert events == ["load-head", "install"]


def test_rollback_passes_the_single_loaded_parent_current_and_backup_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    monkeypatch.setattr(module, "_caddy_transaction_lock", transaction_lock)
    current_fragment = "current hardened fragment\n"
    target_fragment = module.render_caddy_template(
        module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18_087,
    )
    current = hardened_route(
        module,
        fragment=current_fragment,
        upstream=make_upstream(module, marker="1", port=18_088),
        assets=asset_identity(module, "7"),
    )
    target = legacy_route(
        module,
        fragment=target_fragment,
        upstream=make_upstream(module, marker="3", port=18_087),
    )
    parent = parent_chain(
        module,
        tmp_path,
        current=current,
        backup=target,
    )
    requested = parent.transaction.backup_path
    authorized = SimpleNamespace(
        path=requested,
        fragment=target_fragment,
        upstream_port=18_087,
        profile=module.CADDY_PROFILE_LEGACY_V020,
        parent=parent,
    )
    events: list[str] = []

    def load_backup(_runner: object, path: Path) -> object:
        events.append("authorize-current-head")
        assert path == requested
        return authorized

    def install(
        _runner: object,
        fragment: str,
        *,
        label: str,
        operation: str,
        current_route: object,
        installed_route: object,
        parent: object | None,
        caddy_templates: Mapping[str, str] | None = None,
    ) -> Path:
        events.append("install")
        assert fragment == target_fragment
        assert label.startswith("rollback-safety-")
        assert operation == "rollback"
        assert current_route is parent_chain_value.installed
        assert installed_route is parent_chain_value.transaction.backup
        assert parent is parent_chain_value
        assert caddy_templates is None
        return tmp_path / "rollback-safety.conf"

    parent_chain_value = parent
    monkeypatch.setattr(module, "_load_validated_backup", load_backup)
    monkeypatch.setattr(module, "_install_caddy_fragment", install)

    safety = module.rollback_site(
        argparse.Namespace(backup=str(requested)),
        object(),
    )

    assert safety == tmp_path / "rollback-safety.conf"
    assert events == ["authorize-current-head", "install"]


def test_first_switch_uses_the_exact_legacy_bootstrap_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state, verified_candidate, rendered = candidate_fixture(module)
    install_candidate_stubs(
        module,
        monkeypatch,
        state=state,
        verified_candidate=verified_candidate,
    )
    legacy_fragment = module.render_caddy_template(
        module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18_087,
    )
    exact_legacy = legacy_route(
        module,
        fragment=legacy_fragment,
        upstream=make_upstream(module, marker="3", port=18_087),
    )
    events: list[str] = []

    def load_active(_runner: object, *, allow_missing: bool) -> None:
        assert allow_missing is True
        events.append("load-head:none")

    def load_legacy(
        _runner: object,
        *,
        caddy_templates: Mapping[str, str],
    ) -> object:
        events.append("validate-exact-legacy")
        assert caddy_templates is verified_candidate.deployment_assets.caddy_templates
        return exact_legacy

    def install(
        _runner: object,
        fragment: str,
        *,
        operation: str,
        current_route: object,
        installed_route: object,
        parent: object | None,
        **_kwargs: object,
    ) -> Path:
        events.append("install")
        assert fragment == rendered
        assert operation == "switch"
        assert current_route is exact_legacy
        assert parent is None
        assert_candidate_route(
            module,
            installed_route,
            verified_candidate,
            rendered,
        )
        return tmp_path / "bootstrap-backup.conf"

    monkeypatch.setattr(module, "_load_active_caddy_chain", load_active)
    monkeypatch.setattr(
        module,
        "_load_exact_legacy_bootstrap_route",
        load_legacy,
        raising=False,
    )
    monkeypatch.setattr(module, "_install_caddy_fragment", install)

    backup = module.switch_candidate(
        argparse.Namespace(state=str(tmp_path / "candidate.json")),
        object(),
    )

    assert backup == tmp_path / "bootstrap-backup.conf"
    assert events == ["load-head:none", "validate-exact-legacy", "install"]


def test_first_switch_rejects_a_non_frozen_legacy_identity_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state, verified_candidate, _rendered = candidate_fixture(module)
    install_candidate_stubs(
        module,
        monkeypatch,
        state=state,
        verified_candidate=verified_candidate,
    )
    events: list[str] = []

    def load_active(_runner: object, *, allow_missing: bool) -> None:
        assert allow_missing is True
        events.append("load-head:none")

    def reject_legacy(
        _runner: object,
        *,
        caddy_templates: Mapping[str, str],
    ) -> object:
        assert caddy_templates is verified_candidate.deployment_assets.caddy_templates
        events.append("reject-legacy-fingerprint")
        raise module.DeploymentError(
            "legacy upstream does not match the frozen bootstrap identity"
        )

    def reject_install(*_args: object, **_kwargs: object) -> Path:
        events.append("unexpected-install")
        raise AssertionError("an unverified legacy route reached installation")

    monkeypatch.setattr(module, "_load_active_caddy_chain", load_active)
    monkeypatch.setattr(
        module,
        "_load_exact_legacy_bootstrap_route",
        reject_legacy,
        raising=False,
    )
    monkeypatch.setattr(module, "_install_caddy_fragment", reject_install)

    with pytest.raises(module.DeploymentError, match="frozen bootstrap identity"):
        module.switch_candidate(
            argparse.Namespace(state=str(tmp_path / "candidate.json")),
            object(),
        )

    assert events == ["load-head:none", "reject-legacy-fingerprint"]


def test_exact_legacy_bootstrap_loader_binds_fragment_route_marker_and_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fragment = module.render_caddy_template(
        module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18_087,
    )
    upstream = make_upstream(module, marker="3", port=18_087)
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
    events: list[str] = []

    def validate_fragment(
        _runner: object,
        received: str,
        *,
        expected_upstream_port: int | None,
        expected_route_revision: str | None,
        allowed_profiles: frozenset[str],
        caddy_templates: Mapping[str, str],
    ) -> object:
        events.append("validate-fragment")
        assert received == fragment
        assert expected_upstream_port == 18_087
        assert expected_route_revision is None
        assert allowed_profiles == frozenset({module.CADDY_PROFILE_LEGACY_V020})
        assert caddy_templates is templates
        return module.ValidatedCaddyFragment(
            profile=module.CADDY_PROFILE_LEGACY_V020,
            upstream_port=18_087,
            route_revision=None,
        )

    monkeypatch.setattr(module, "_read_current_site", lambda _runner: fragment)
    monkeypatch.setattr(module, "_validate_managed_fragment", validate_fragment)
    monkeypatch.setattr(
        module,
        "_assert_active_caddy_route",
        lambda _runner, received: (
            events.append("active-route")
            if received == fragment
            else pytest.fail("wrong fragment")
        ),
    )

    def load_upstream(
        _runner: object,
        port: int,
        *,
        require_exclusive_network: bool,
    ) -> object:
        assert port == 18_087
        assert require_exclusive_network is False
        events.append("load-upstream")
        return upstream

    monkeypatch.setattr(
        module,
        "_upstream_identity_for_host_port",
        load_upstream,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "LEGACY_UPSTREAM_FINGERPRINT",
        module._upstream_identity_fingerprint(upstream),
    )
    monkeypatch.setattr(
        module,
        "_smoke_caddy",
        lambda route: (
            events.append("smoke")
            if route.upstream is upstream
            else pytest.fail("wrong route")
        ),
    )
    monkeypatch.setattr(
        module,
        "_assert_route_upstream_ready",
        lambda _runner, received: (
            events.append("upstream-ready")
            if received.upstream is upstream
            and received.profile == module.CADDY_PROFILE_LEGACY_V020
            else pytest.fail("wrong route")
        ),
        raising=False,
    )

    route = module._load_exact_legacy_bootstrap_route(
        object(),
        caddy_templates=templates,
    )

    assert route == legacy_route(module, fragment=fragment, upstream=upstream)
    assert events == [
        "validate-fragment",
        "active-route",
        "load-upstream",
        "smoke",
        "upstream-ready",
    ]


def test_exact_legacy_bootstrap_loader_rejects_fragment_or_upstream_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fragment = module.render_caddy_template(
        module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18_087,
    )
    assert hashlib.sha256(fragment.encode("utf-8")).hexdigest() == (
        "740ab123464b07d8e6460c974abc901c4025fc08f34a955402ba5db574994e3a"
    )
    templates = {
        module.CADDY_PROFILE_HARDENED: "hardened",
        module.CADDY_PROFILE_LEGACY_V020: "legacy",
    }
    monkeypatch.setattr(
        module,
        "_read_current_site",
        lambda _runner: fragment + "# changed\n",
    )

    with pytest.raises(module.DeploymentError, match="legacy.*fragment|frozen"):
        module._load_exact_legacy_bootstrap_route(
            object(),
            caddy_templates=templates,
        )

    monkeypatch.setattr(module, "_read_current_site", lambda _runner: fragment)
    monkeypatch.setattr(
        module,
        "_validate_managed_fragment",
        lambda *_args, **_kwargs: module.ValidatedCaddyFragment(
            profile=module.CADDY_PROFILE_LEGACY_V020,
            upstream_port=18_087,
            route_revision=None,
        ),
    )
    monkeypatch.setattr(module, "_assert_active_caddy_route", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_upstream_identity_for_host_port",
        lambda *_args, **_kwargs: make_upstream(module, marker="3", port=18_087),
        raising=False,
    )

    with pytest.raises(module.DeploymentError, match="frozen bootstrap identity"):
        module._load_exact_legacy_bootstrap_route(
            object(),
            caddy_templates=templates,
        )
