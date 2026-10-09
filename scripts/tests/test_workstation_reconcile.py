"""Crash reconciliation contracts for workstation Caddy transactions."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
BOOTSTRAP_ID = "b" * 64
PARENT_TRANSACTION_ID = "20261009T110000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TRANSACTION_ID = "20261009T120000Z-0123456789abcdef0123456789abcdef"
OTHER_TRANSACTION_ID = "20261009T130000Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_reconcile",
        DEPLOY_TOOL,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stable_json(value: object) -> str:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def make_route(
    module: ModuleType,
    fragment: str,
    *,
    marker: str,
    port: int,
    profile: str,
) -> object:
    upstream = make_upstream(module, marker=marker, port=port)
    if profile == module.CADDY_PROFILE_LEGACY_V020:
        revision = None
        assets = None
    else:
        asset_payload = {
            "schema": module.DEPLOYMENT_ASSET_SCHEMA,
            "sha256": {path: marker * 64 for path in module.DEPLOYMENT_ASSET_PATHS},
        }
        revision = module._route_revision(profile, upstream, asset_payload)
        assets = MappingProxyType(module._validate_deployment_assets(asset_payload))
    return module.RouteState(
        fragment_sha256=text_sha256(fragment),
        profile=profile,
        route_revision=revision,
        upstream=upstream,
        deployment_assets=assets,
    )


def make_transaction(
    module: ModuleType,
    *,
    transaction_id: str,
    bootstrap_id: str,
    parent: object | None,
    backup: object,
    installed: object,
) -> object:
    backup_path = module.CADDY_TRANSACTION_DIRECTORY / (
        f"commerce-ops-desk.{transaction_id}.conf"
    )
    ledger_path = backup_path.with_suffix(".json")
    parent_transaction_id = None if parent is None else parent.transaction_id
    parent_active_sha256 = None if parent is None else parent.active_sha256
    record = {
        "schema": module.CADDY_TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "operation": "switch",
        "bootstrap_id": bootstrap_id,
        "parent": {
            "transaction_id": parent_transaction_id,
            "active_sha256": parent_active_sha256,
        },
        "site": {
            "path": str(module.CADDY_SITE),
            "host": module.CADDY_HOST,
        },
        "backup": {
            "path": str(backup_path),
            **module._route_state_payload(backup),
        },
        "installed": module._route_state_payload(installed),
    }
    ledger_text = stable_json(record)
    return module.PersistedCaddyTransaction(
        transaction_id=transaction_id,
        operation="switch",
        bootstrap_id=bootstrap_id,
        backup_path=backup_path,
        ledger_path=ledger_path,
        ledger_text=ledger_text,
        ledger_sha256=text_sha256(ledger_text),
        backup=backup,
        installed=installed,
    )


class ReconcileHarness:
    """Model only the trusted state and externally meaningful verification steps."""

    def __init__(
        self,
        module: ModuleType,
        tmp_path: Path,
        *,
        with_parent: bool,
    ) -> None:
        self.module = module
        self.transaction_directory = tmp_path / "root-caddy-transactions"
        module.CADDY_TRANSACTION_DIRECTORY = self.transaction_directory
        module.CADDY_ACTIVE_STATE = self.transaction_directory / "active.json"
        self.backup_fragment = "trusted-backup-fragment\n"
        self.installed_fragment = "verified-installed-fragment\n"
        backup_profile = (
            module.CADDY_PROFILE_HARDENED
            if with_parent
            else module.CADDY_PROFILE_LEGACY_V020
        )
        self.backup_route = make_route(
            module,
            self.backup_fragment,
            marker="1",
            port=18_087,
            profile=backup_profile,
        )
        self.installed_route = make_route(
            module,
            self.installed_fragment,
            marker="2",
            port=18_088,
            profile=module.CADDY_PROFILE_HARDENED,
        )
        self.parent = self._make_parent() if with_parent else None
        self.transaction = make_transaction(
            module,
            transaction_id=TRANSACTION_ID,
            bootstrap_id=BOOTSTRAP_ID,
            parent=self.parent,
            backup=self.backup_route,
            installed=self.installed_route,
        )
        self.proposed_head = module._active_chain_for_transaction(self.transaction)
        self.active_head = self.parent
        self.current_fragment = self.installed_fragment
        self.events: list[str] = []

    def _make_parent(self) -> object:
        transaction = make_transaction(
            self.module,
            transaction_id=PARENT_TRANSACTION_ID,
            bootstrap_id=BOOTSTRAP_ID,
            parent=None,
            backup=self.backup_route,
            installed=self.backup_route,
        )
        return self.module._active_chain_for_transaction(transaction)

    def make_other_head(self) -> object:
        transaction = make_transaction(
            self.module,
            transaction_id=OTHER_TRANSACTION_ID,
            bootstrap_id=BOOTSTRAP_ID,
            parent=None,
            backup=self.backup_route,
            installed=self.backup_route,
        )
        return self.module._active_chain_for_transaction(transaction)

    def _stage(self) -> str:
        if self.current_fragment == self.installed_fragment:
            return "installed"
        if self.current_fragment == self.backup_fragment:
            return "backup"
        return "third"

    def _route_for_stage(self) -> object:
        stage = self._stage()
        if stage == "installed":
            return self.installed_route
        if stage == "backup":
            return self.backup_route
        raise AssertionError(
            "an unrelated site must be refused before route verification"
        )

    @contextmanager
    def lock(self, _runner: object) -> Iterator[None]:
        self.events.append("lock-enter")
        try:
            yield
        finally:
            self.events.append("lock-exit")

    def load_transaction(
        self,
        _runner: object,
        ledger_path: Path,
        *,
        validate_fragment: bool,
    ) -> tuple[object, str]:
        self.events.append("load-transaction")
        assert ledger_path == self.transaction.ledger_path
        assert validate_fragment is True
        return self.transaction, self.backup_fragment

    def load_active(
        self,
        _runner: object,
        *,
        allow_missing: bool,
        validate_fragment: bool = True,
    ) -> object | None:
        self.events.append("load-head")
        assert allow_missing is True
        assert validate_fragment is True
        return self.active_head

    def assert_transaction(
        self,
        _runner: object,
        transaction: object,
        *,
        expected_parent: object | None,
    ) -> None:
        self.events.append("bind-parent")
        assert transaction is self.transaction
        if expected_parent is not self.parent:
            raise self.module.DeploymentError(
                "transaction parent does not match the current head"
            )

    def read_site(self, _runner: object) -> str:
        self.events.append("read-site")
        return self.current_fragment

    def validate_fragment(
        self,
        _runner: object,
        fragment: str,
        *,
        expected_upstream_port: int | None,
        expected_route_revision: str | None = None,
        allowed_profiles: frozenset[str],
        caddy_templates: Mapping[str, str] | None = None,
    ) -> object:
        del caddy_templates
        if fragment == self.installed_fragment:
            route = self.installed_route
            stage = "installed"
        elif fragment == self.backup_fragment:
            route = self.backup_route
            stage = "backup"
        else:
            raise AssertionError(
                "an unrelated site must be refused before route verification"
            )
        assert route.profile in allowed_profiles
        assert expected_upstream_port == route.upstream.host_port
        assert expected_route_revision == route.route_revision
        self.events.append(f"validate-fragment:{stage}")
        return self.module.ValidatedCaddyFragment(
            profile=route.profile,
            upstream_port=route.upstream.host_port,
            route_revision=route.route_revision,
        )

    def wait_for_healthy(
        self,
        _runner: object,
        container_name: str,
        *,
        timeout: float,
    ) -> None:
        if container_name == self.installed_route.upstream.container_name:
            stage = "installed"
        elif container_name == self.backup_route.upstream.container_name:
            stage = "backup"
        else:
            raise AssertionError("unexpected upstream health target")
        assert timeout > 0
        self.events.append(f"healthy:{stage}")

    def validate_caddy(self, _runner: object) -> None:
        self.events.append(f"validate-caddy:{self._stage()}")

    def reload_caddy(self, _runner: object) -> None:
        self.events.append(f"reload:{self._stage()}")

    def assert_active_route(self, _runner: object, fragment: str) -> None:
        assert fragment == self.current_fragment
        self.events.append(f"active-route:{self._stage()}")

    def smoke(self, route: object) -> None:
        assert route is self._route_for_stage()
        self.events.append(f"smoke:{self._stage()}")

    def assert_upstream(self, _runner: object, upstream: object) -> None:
        if upstream == self.installed_route.upstream:
            stage = "installed"
        elif upstream == self.backup_route.upstream:
            stage = "backup"
        else:
            raise AssertionError("unexpected upstream identity target")
        self.events.append(f"upstream:{stage}")

    def commit(
        self,
        _runner: object,
        transaction: object,
        *,
        expected_parent: object | None,
    ) -> object:
        self.events.append("commit")
        assert transaction is self.transaction
        assert expected_parent is self.parent
        assert self._stage() == "installed"
        self.active_head = self.proposed_head
        return self.proposed_head

    def install_site(
        self,
        _runner: object,
        content: str,
        *,
        expected_sha256: str,
        expected_current_sha256: str,
    ) -> None:
        assert content == self.backup_fragment
        assert expected_sha256 == self.backup_route.fragment_sha256
        assert expected_current_sha256 == text_sha256(self.current_fragment)
        self.events.append("install-site:backup")
        self.current_fragment = content

    def persist(
        self,
        _runner: object,
        original: str,
        *,
        operation: str,
        parent: object | None,
        backup: object,
        installed: object,
    ) -> object:
        self.events.append("persist")
        assert original == self.backup_fragment
        assert operation == "switch"
        assert parent is self.parent
        assert backup is self.backup_route
        assert installed is self.installed_route
        return self.transaction


def install_reconcile_harness(
    module: ModuleType,
    harness: ReconcileHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_caddy_transaction_lock", harness.lock)
    monkeypatch.setattr(
        module,
        "_load_persisted_caddy_transaction",
        harness.load_transaction,
    )
    monkeypatch.setattr(module, "_load_active_caddy_chain", harness.load_active)
    monkeypatch.setattr(
        module,
        "_load_active_caddy_chain_with_fragment_policy",
        harness.load_active,
    )
    monkeypatch.setattr(
        module,
        "_assert_persisted_transaction",
        harness.assert_transaction,
    )
    monkeypatch.setattr(module, "_read_current_site", harness.read_site)
    monkeypatch.setattr(
        module,
        "_validate_managed_fragment",
        harness.validate_fragment,
    )
    monkeypatch.setattr(module, "_wait_for_healthy", harness.wait_for_healthy)
    monkeypatch.setattr(module, "_validate_caddy", harness.validate_caddy)
    monkeypatch.setattr(module, "_reload_caddy", harness.reload_caddy)
    monkeypatch.setattr(
        module,
        "_assert_active_caddy_route",
        harness.assert_active_route,
    )
    monkeypatch.setattr(module, "_smoke_caddy", harness.smoke)
    monkeypatch.setattr(
        module,
        "_assert_upstream_identity_current",
        harness.assert_upstream,
    )
    monkeypatch.setattr(module, "_commit_active_caddy_state", harness.commit)
    monkeypatch.setattr(module, "_atomic_install_site", harness.install_site)


def invoke_reconcile(module: ModuleType, harness: ReconcileHarness) -> object:
    return module.reconcile_transaction(
        argparse.Namespace(transaction=str(harness.transaction.ledger_path)),
        object(),
    )


def assert_full_route_reverification(
    harness: ReconcileHarness,
    *,
    stage: str,
    committed: bool,
) -> None:
    assert harness.events[0] == "lock-enter"
    assert harness.events[-1] == "lock-exit"
    for event in (
        "load-transaction",
        "load-head",
        "read-site",
        f"validate-fragment:{stage}",
        f"healthy:{stage}",
        f"validate-caddy:{stage}",
        f"reload:{stage}",
        f"active-route:{stage}",
        f"smoke:{stage}",
    ):
        expected_count = 2 if event == f"healthy:{stage}" else 1
        assert harness.events.count(event) == expected_count
    assert harness.events.count(f"upstream:{stage}") == 2
    ordered_verification = [
        f"healthy:{stage}",
        f"upstream:{stage}",
        f"validate-caddy:{stage}",
        f"reload:{stage}",
        f"active-route:{stage}",
        f"smoke:{stage}",
        f"healthy:{stage}",
        f"upstream:{stage}",
    ]
    positions: list[int] = []
    cursor = 0
    for event in ordered_verification:
        position = harness.events.index(event, cursor)
        positions.append(position)
        cursor = position + 1
    assert harness.events.index(f"validate-fragment:{stage}") < harness.events.index(
        f"reload:{stage}"
    )
    if committed:
        assert harness.events.count("commit") == 1
        assert harness.events.index("commit") > positions[-1]
    else:
        assert "commit" not in harness.events


@pytest.mark.parametrize("with_parent", [False, True], ids=("bootstrap", "chained"))
def test_reconcile_installed_snapshot_reverifies_everything_before_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_parent: bool,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=with_parent)
    install_reconcile_harness(module, harness, monkeypatch)

    invoke_reconcile(module, harness)

    assert harness.active_head is harness.proposed_head
    assert harness.events.count("bind-parent") == 1
    assert_full_route_reverification(
        harness,
        stage="installed",
        committed=True,
    )


@pytest.mark.parametrize("with_parent", [False, True], ids=("bootstrap", "chained"))
def test_reconcile_backup_snapshot_reverifies_old_route_and_keeps_parent_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_parent: bool,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=with_parent)
    harness.current_fragment = harness.backup_fragment
    original_head = harness.active_head
    install_reconcile_harness(module, harness, monkeypatch)

    invoke_reconcile(module, harness)

    assert harness.active_head is original_head
    assert harness.events.count("bind-parent") == 1
    assert_full_route_reverification(
        harness,
        stage="backup",
        committed=False,
    )


def test_reconcile_rejects_a_transaction_whose_parent_is_not_the_current_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    other_head = harness.make_other_head()
    harness.active_head = other_head
    install_reconcile_harness(module, harness, monkeypatch)

    with pytest.raises(module.DeploymentError, match="parent|head"):
        invoke_reconcile(module, harness)

    assert harness.active_head is other_head
    assert harness.events[0] == "lock-enter"
    assert harness.events[-1] == "lock-exit"
    assert "bind-parent" in harness.events
    assert not any(
        event.startswith(("healthy:", "reload:", "smoke:", "upstream:"))
        for event in harness.events
    )
    assert "commit" not in harness.events


def test_reconcile_rejects_a_site_matching_neither_transaction_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    harness.current_fragment = "unrelated-privileged-site-state\n"
    original_head = harness.active_head
    install_reconcile_harness(module, harness, monkeypatch)

    with pytest.raises(module.DeploymentError, match="site|fragment|state|drift"):
        invoke_reconcile(module, harness)

    assert harness.active_head is original_head
    assert harness.events[0] == "lock-enter"
    assert harness.events[-1] == "lock-exit"
    assert harness.events.count("read-site") == 1
    assert not any(
        event.startswith(
            ("validate-fragment:", "healthy:", "reload:", "smoke:", "upstream:")
        )
        for event in harness.events
    )
    assert "commit" not in harness.events


def test_reconcile_is_idempotent_for_the_exact_proposed_head_but_reverifies_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    harness.active_head = harness.proposed_head
    install_reconcile_harness(module, harness, monkeypatch)

    invoke_reconcile(module, harness)

    assert harness.active_head is harness.proposed_head
    assert "bind-parent" not in harness.events
    assert_full_route_reverification(
        harness,
        stage="installed",
        committed=False,
    )


def test_reconcile_restores_the_parent_route_when_orphan_upstream_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    original_head = harness.active_head
    install_reconcile_harness(module, harness, monkeypatch)
    original_wait = harness.wait_for_healthy

    def fail_installed_health(
        runner: object,
        container_name: str,
        *,
        timeout: float,
    ) -> None:
        if container_name == harness.installed_route.upstream.container_name:
            harness.events.append("healthy:installed")
            raise module.DeploymentError("orphan upstream is stopped")
        original_wait(
            runner,
            container_name,
            timeout=timeout,
        )

    monkeypatch.setattr(module, "_wait_for_healthy", fail_installed_health)

    invoke_reconcile(module, harness)

    assert harness.active_head is original_head
    assert harness.current_fragment == harness.backup_fragment
    assert "reload:installed" not in harness.events
    assert harness.events.count("load-head") == 2
    assert harness.events.count("read-site") >= 2
    restore_order = [
        "validate-fragment:backup",
        "healthy:backup",
        "upstream:backup",
        "install-site:backup",
        "validate-caddy:backup",
        "reload:backup",
        "active-route:backup",
        "smoke:backup",
        "upstream:backup",
    ]
    cursor = 0
    for event in restore_order:
        position = harness.events.index(event, cursor)
        cursor = position + 1
    assert "commit" not in harness.events


def test_reconcile_reload_timeout_is_indeterminate_and_never_restores_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    install_reconcile_harness(module, harness, monkeypatch)

    def timeout_reload(_runner: object) -> None:
        harness.events.append(f"reload:{harness._stage()}")
        error = module.DeploymentError("injected reload timeout")
        error.__cause__ = subprocess.TimeoutExpired(cmd="systemctl", timeout=60)
        raise error

    monkeypatch.setattr(module, "_reload_caddy", timeout_reload)

    with pytest.raises(
        module.DeploymentError,
        match="indeterminate|restoration refused|timed out",
    ):
        invoke_reconcile(module, harness)

    assert harness.current_fragment == harness.installed_fragment
    assert harness.active_head is harness.parent
    assert "install-site:backup" not in harness.events
    assert "commit" not in harness.events


def test_reconcile_refuses_to_restore_an_unhealthy_backup_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    original_head = harness.active_head
    install_reconcile_harness(module, harness, monkeypatch)

    def fail_all_health(
        _runner: object,
        container_name: str,
        *,
        timeout: float,
    ) -> None:
        assert timeout > 0
        stage = (
            "installed"
            if container_name == harness.installed_route.upstream.container_name
            else "backup"
        )
        harness.events.append(f"healthy:{stage}")
        raise module.DeploymentError(f"{stage} upstream is stopped")

    monkeypatch.setattr(module, "_wait_for_healthy", fail_all_health)

    with pytest.raises(
        module.DeploymentError,
        match="could not verify.*restoration also failed",
    ):
        invoke_reconcile(module, harness)

    assert harness.active_head is original_head
    assert harness.current_fragment == harness.installed_fragment
    assert "install-site:backup" not in harness.events
    assert "reload:installed" not in harness.events
    assert "reload:backup" not in harness.events
    assert "commit" not in harness.events


def test_parser_and_main_expose_reconcile_with_an_exact_transaction_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    ledger_path = tmp_path / "root-caddy-transactions" / "transaction.json"
    parsed = module.build_parser().parse_args(
        ["reconcile", "--transaction", str(ledger_path)]
    )

    assert parsed.command == "reconcile"
    assert parsed.transaction == str(ledger_path)

    runner = object()
    calls: list[tuple[argparse.Namespace, object]] = []
    monkeypatch.setattr(module, "CommandRunner", lambda: runner)

    def reconcile(arguments: argparse.Namespace, received_runner: object) -> None:
        calls.append((arguments, received_runner))

    monkeypatch.setattr(
        module,
        "reconcile_transaction",
        reconcile,
        raising=False,
    )

    assert module.main(["reconcile", "--transaction", str(ledger_path)]) == 0
    assert len(calls) == 1
    arguments, received_runner = calls[0]
    assert received_runner is runner
    assert arguments.command == "reconcile"
    assert arguments.transaction == str(ledger_path)


class SimulatedProcessDeath(BaseException):
    """Model SIGKILL-like interruption outside the Exception recovery path."""


def test_install_flushes_reconcile_ledger_path_before_a_fatal_site_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = ReconcileHarness(module, tmp_path, with_parent=True)
    harness.current_fragment = harness.backup_fragment
    install_reconcile_harness(module, harness, monkeypatch)
    monkeypatch.setattr(module, "_persist_caddy_transaction", harness.persist)
    announcements: list[tuple[str, bool]] = []
    observed_before_replace = {"path": False, "flush": False}

    def record_print(
        *values: object,
        sep: str = " ",
        end: str = "\n",
        file: object | None = None,
        flush: bool = False,
    ) -> None:
        del file
        announcements.append((sep.join(str(value) for value in values) + end, flush))
        harness.events.append("announce-ledger")

    monkeypatch.setattr(module, "print", record_print, raising=False)

    def replace_then_die(
        _runner: object,
        content: str,
        *,
        expected_sha256: str,
        expected_current_sha256: str,
    ) -> None:
        assert content == harness.installed_fragment
        assert expected_sha256 == text_sha256(content)
        assert expected_current_sha256 == text_sha256(harness.current_fragment)
        if announcements:
            announcement, flushed = announcements[-1]
            observed_before_replace["path"] = (
                str(harness.transaction.ledger_path) in announcement
            )
            observed_before_replace["flush"] = flushed
        harness.events.append("install-site")
        harness.current_fragment = content
        raise SimulatedProcessDeath("process terminated after site replacement")

    monkeypatch.setattr(module, "_atomic_install_site", replace_then_die)

    with pytest.raises(SimulatedProcessDeath, match="after site replacement"):
        module._install_caddy_fragment(
            object(),
            harness.installed_fragment,
            label="candidate",
            operation="switch",
            current_route=harness.backup_route,
            installed_route=harness.installed_route,
            parent=harness.parent,
        )

    assert harness.current_fragment == harness.installed_fragment
    assert harness.active_head is harness.parent
    assert str(harness.transaction.ledger_path) in "".join(
        announcement for announcement, _flushed in announcements
    )
    assert observed_before_replace == {"path": True, "flush": True}
    assert all(flushed for _announcement, flushed in announcements)
    assert harness.events.index("persist") < harness.events.index("announce-ledger")
    assert harness.events.index("announce-ledger") < harness.events.index(
        "install-site"
    )
