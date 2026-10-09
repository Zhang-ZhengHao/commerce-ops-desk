"""Transaction-v3 ordering and ambiguous-commit contracts for Caddy changes."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
BOOTSTRAP_ID = "b" * 64
PARENT_TRANSACTION_ID = "20261009T110000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TRANSACTION_ID = "20261009T120000Z-0123456789abcdef0123456789abcdef"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_transaction_v3", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_upstream(module: ModuleType, *, marker: str, port: int) -> object:
    source_sha = marker * 40
    return module._validate_upstream_identity_payload(
        {
            "schema": 1,
            "docker_daemon_id": f"daemon-{marker}",
            "container_id": marker * 64,
            "container_name": f"commerce-ops-{marker}",
            "image_id": "sha256:" + marker * 64,
            "image_reference": f"commerce-ops-desk:{source_sha}",
            "source_sha": source_sha,
            "host_port": port,
            "data_path": f"/home/deploy/apps/commerce-ops-desk/data-{marker}",
            "data_device": 64_769,
            "data_inode": 55_451_000 + port,
            "network_name": f"commerce-ops-{marker}_default",
            "network_id": marker * 64,
            "network_endpoint_id": ("c" if marker != "c" else "d") * 64,
            "runtime_sha256": ("d" if marker != "d" else "e") * 64,
        }
    )


def make_route(
    module: ModuleType,
    fragment: str,
    *,
    marker: str,
    port: int,
) -> object:
    upstream = make_upstream(module, marker=marker, port=port)
    assets_payload = {
        "schema": 1,
        "sha256": {path: marker * 64 for path in module.DEPLOYMENT_ASSET_PATHS},
    }
    revision = module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        upstream,
        assets_payload,
    )
    assets = MappingProxyType(module._validate_deployment_assets(assets_payload))
    return module.RouteState(
        fragment_sha256=hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
        profile=module.CADDY_PROFILE_HARDENED,
        route_revision=revision,
        upstream=upstream,
        deployment_assets=assets,
    )


def make_parent_chain(
    module: ModuleType, tmp_path: Path, current_route: object
) -> object:
    ledger_text = '{"transaction":"parent"}\n'
    parent_transaction = module.PersistedCaddyTransaction(
        transaction_id=PARENT_TRANSACTION_ID,
        operation="switch",
        bootstrap_id=BOOTSTRAP_ID,
        backup_path=tmp_path / "parent-backup.conf",
        ledger_path=tmp_path / "parent-ledger.json",
        ledger_text=ledger_text,
        ledger_sha256=hashlib.sha256(ledger_text.encode("utf-8")).hexdigest(),
        backup=current_route,
        installed=current_route,
    )
    active_text = '{"transaction":"parent"}\n'
    return module.ActiveCaddyChain(
        active_text=active_text,
        active_sha256=hashlib.sha256(active_text.encode("utf-8")).hexdigest(),
        bootstrap_id=BOOTSTRAP_ID,
        transaction_id=PARENT_TRANSACTION_ID,
        installed=current_route,
        transaction=parent_transaction,
    )


class TransactionV3Harness:
    """Expose only security-relevant side effects of one install transaction."""

    def __init__(
        self,
        module: ModuleType,
        tmp_path: Path,
        *,
        with_parent: bool,
    ) -> None:
        self.module = module
        self.old_fragment = "old-fragment\n"
        self.new_fragment = "new-fragment\n"
        self.current_route = make_route(
            module,
            self.old_fragment,
            marker="1",
            port=18_087,
        )
        self.installed_route = make_route(
            module,
            self.new_fragment,
            marker="2",
            port=18_088,
        )
        self.parent = (
            make_parent_chain(module, tmp_path, self.current_route)
            if with_parent
            else None
        )
        self.backup = tmp_path / "root-transactions" / "trusted-backup.conf"
        ledger_text = '{"transaction":"candidate"}\n'
        self.transaction = module.PersistedCaddyTransaction(
            transaction_id=TRANSACTION_ID,
            operation="switch",
            bootstrap_id=BOOTSTRAP_ID,
            backup_path=self.backup,
            ledger_path=self.backup.with_suffix(".json"),
            ledger_text=ledger_text,
            ledger_sha256=hashlib.sha256(ledger_text.encode("utf-8")).hexdigest(),
            backup=self.current_route,
            installed=self.installed_route,
        )
        active_text = '{"transaction":"candidate"}\n'
        self.new_head = module.ActiveCaddyChain(
            active_text=active_text,
            active_sha256=hashlib.sha256(active_text.encode("utf-8")).hexdigest(),
            bootstrap_id=BOOTSTRAP_ID,
            transaction_id=TRANSACTION_ID,
            installed=self.installed_route,
            transaction=self.transaction,
        )
        self.current_fragment = self.old_fragment
        self.active_head = self.parent
        self.events: list[str] = []
        self.fail_stage: str | None = None
        self.commit_outcome = "success"

    def _stage(self) -> str:
        return "new" if self.current_fragment == self.new_fragment else "old"

    def _raise_at(self, stage: str) -> None:
        if self._stage() == "new" and self.fail_stage == stage:
            raise self.module.DeploymentError(f"injected {stage} failure")

    def validate_fragment(
        self,
        _runner: object,
        fragment: str,
        *,
        expected_upstream_port: int | None,
        expected_route_revision: str | None = None,
        allowed_profiles: frozenset[str],
        caddy_templates: object | None = None,
    ) -> object:
        del caddy_templates
        route = (
            self.current_route
            if fragment == self.old_fragment
            else self.installed_route
        )
        assert route.profile in allowed_profiles
        if expected_upstream_port is not None:
            assert expected_upstream_port == route.upstream.host_port
        if expected_route_revision is not None:
            assert expected_route_revision == route.route_revision
        return self.module.ValidatedCaddyFragment(
            profile=route.profile,
            upstream_port=route.upstream.host_port,
            route_revision=route.route_revision,
        )

    def read_current(self, _runner: object) -> str:
        return self.current_fragment

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
        assert original == self.old_fragment
        assert operation == "switch"
        assert parent is self.parent
        assert backup is self.current_route
        assert installed is self.installed_route
        return self.transaction

    def install(
        self,
        _runner: object,
        content: str,
        *,
        expected_sha256: str,
        expected_current_sha256: str,
    ) -> None:
        assert hashlib.sha256(content.encode("utf-8")).hexdigest() == expected_sha256
        assert (
            hashlib.sha256(self.current_fragment.encode("utf-8")).hexdigest()
            == expected_current_sha256
        )
        if content == self.old_fragment:
            self.events.append("install:old")
            self.current_fragment = self.old_fragment
            return
        assert content == self.new_fragment
        self.events.append("install:new")
        self.current_fragment = self.new_fragment

    def validate_caddy(self, _runner: object) -> None:
        self.events.append(f"validate:{self._stage()}")
        self._raise_at("validate")

    def reload_caddy(self, _runner: object) -> None:
        self.events.append(f"reload:{self._stage()}")
        self._raise_at("reload")

    def assert_active_route(self, _runner: object, fragment: str) -> None:
        stage = self._stage()
        self.events.append(f"active-route:{stage}")
        expected = self.new_fragment if stage == "new" else self.old_fragment
        assert fragment == expected
        self._raise_at("active-route")

    def smoke(self, route: object) -> None:
        stage = self._stage()
        self.events.append(f"smoke:{stage}")
        expected = self.installed_route if stage == "new" else self.current_route
        assert route is expected
        self._raise_at("smoke")

    def assert_upstream(self, _runner: object, upstream: object) -> None:
        stage = self._stage()
        target = "installed" if upstream == self.installed_route.upstream else "current"
        self.events.append(f"upstream:{target}:{stage}")
        expected_route = (
            self.installed_route if target == "installed" else self.current_route
        )
        assert upstream == expected_route.upstream
        if target == "installed" and stage == "new":
            self._raise_at("upstream")

    def wait_for_healthy(
        self,
        _runner: object,
        container_name: str,
        *,
        timeout: float,
    ) -> None:
        stage = self._stage()
        target = (
            "installed"
            if container_name == self.installed_route.upstream.container_name
            else "current"
        )
        self.events.append(f"healthy:{target}:{stage}")
        expected_route = (
            self.installed_route if target == "installed" else self.current_route
        )
        assert container_name == expected_route.upstream.container_name
        assert timeout == 15.0

    def commit(
        self,
        _runner: object,
        transaction: object,
        expected_parent: object | None,
    ) -> object:
        self.events.append("commit")
        assert transaction is self.transaction
        assert expected_parent is self.parent
        if self.commit_outcome == "before":
            raise self.module.DeploymentError("injected pre-publish commit failure")
        if self.commit_outcome == "timeout":
            error = self.module.DeploymentError("injected active commit timeout")
            error.__cause__ = subprocess.TimeoutExpired(cmd="sudo", timeout=60)
            raise error
        if self.commit_outcome == "indeterminate-timeout":
            raise self.module.IndeterminateCaddyMutationError(
                "injected timeout with failed observation"
            )
        self.active_head = self.new_head
        if self.commit_outcome == "after-forged":
            forged_ledger = '{"transaction":"forged"}\n'
            forged_transaction = replace(
                self.transaction,
                ledger_text=forged_ledger,
                ledger_sha256=hashlib.sha256(forged_ledger.encode("utf-8")).hexdigest(),
            )
            self.active_head = replace(
                self.new_head,
                transaction=forged_transaction,
            )
            raise self.module.DeploymentError("injected forged post-publish head")
        if self.commit_outcome == "after":
            raise self.module.DeploymentError("injected post-publish commit failure")
        return self.new_head

    def load_active(
        self,
        _runner: object,
        *,
        allow_missing: bool,
    ) -> object | None:
        self.events.append("observe-head")
        assert allow_missing is True
        return self.active_head


def install_harness(
    module: ModuleType,
    harness: TransactionV3Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "_validate_managed_fragment", harness.validate_fragment)
    monkeypatch.setattr(module, "_read_current_site", harness.read_current)
    monkeypatch.setattr(module, "_persist_caddy_transaction", harness.persist)
    monkeypatch.setattr(module, "_atomic_install_site", harness.install)
    monkeypatch.setattr(module, "_validate_caddy", harness.validate_caddy)
    monkeypatch.setattr(module, "_reload_caddy", harness.reload_caddy)
    monkeypatch.setattr(
        module, "_assert_active_caddy_route", harness.assert_active_route
    )
    monkeypatch.setattr(module, "_smoke_caddy", harness.smoke)
    monkeypatch.setattr(
        module,
        "_assert_upstream_identity_current",
        harness.assert_upstream,
    )
    monkeypatch.setattr(module, "_wait_for_healthy", harness.wait_for_healthy)
    monkeypatch.setattr(
        module,
        "_commit_active_caddy_state",
        harness.commit,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_load_active_caddy_chain",
        harness.load_active,
        raising=False,
    )


def invoke_install(module: ModuleType, harness: TransactionV3Harness) -> Path:
    return module._install_caddy_fragment(
        object(),
        harness.new_fragment,
        label="candidate",
        operation="switch",
        current_route=harness.current_route,
        installed_route=harness.installed_route,
        parent=harness.parent,
    )


SUCCESS_EVENTS = [
    "healthy:installed:old",
    "upstream:installed:old",
    "persist",
    "install:new",
    "validate:new",
    "reload:new",
    "active-route:new",
    "smoke:new",
    "healthy:installed:new",
    "upstream:installed:new",
    "commit",
]
RECOVERY_EVENTS = [
    "healthy:current:new",
    "upstream:current:new",
    "install:old",
    "validate:old",
    "reload:old",
    "active-route:old",
    "smoke:old",
    "healthy:current:old",
    "upstream:current:old",
]


@pytest.mark.parametrize("with_parent", [False, True], ids=("bootstrap", "chained"))
def test_success_uses_the_strict_transaction_order_and_returns_the_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_parent: bool,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=with_parent)
    install_harness(module, harness, tmp_path, monkeypatch)

    backup = invoke_install(module, harness)

    assert backup == harness.backup
    assert harness.events == SUCCESS_EVENTS
    assert harness.current_fragment == harness.new_fragment
    assert harness.active_head is harness.new_head


@pytest.mark.parametrize(
    "failure_stage",
    ["validate", "reload", "active-route", "smoke", "upstream"],
)
def test_each_precommit_verification_failure_restores_the_route_and_keeps_the_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    harness.fail_stage = failure_stage
    install_harness(module, harness, tmp_path, monkeypatch)
    failing_event = (
        "upstream:installed:new"
        if failure_stage == "upstream"
        else f"{failure_stage}:new"
    )

    with pytest.raises(module.DeploymentError, match=failure_stage):
        invoke_install(module, harness)

    assert harness.events == (
        SUCCESS_EVENTS[: SUCCESS_EVENTS.index(failing_event) + 1]
        + ["observe-head"]
        + RECOVERY_EVENTS
    )
    assert harness.current_fragment == harness.old_fragment
    assert harness.active_head is harness.parent
    assert "commit" not in harness.events


def test_unhealthy_rollback_target_is_refused_before_persist_or_site_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    install_harness(module, harness, tmp_path, monkeypatch)

    def reject_stopped_target(
        _runner: object,
        container_name: str,
        *,
        timeout: float,
    ) -> None:
        harness.events.append("healthy:refused")
        assert container_name == harness.installed_route.upstream.container_name
        assert timeout == 15.0
        raise module.DeploymentError("target container is stopped")

    monkeypatch.setattr(module, "_wait_for_healthy", reject_stopped_target)

    with pytest.raises(module.DeploymentError, match="stopped"):
        invoke_install(module, harness)

    assert harness.events == ["healthy:refused"]
    assert harness.current_fragment == harness.old_fragment
    assert harness.active_head is harness.parent


def test_target_identity_is_verified_before_persist_or_site_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    install_harness(module, harness, tmp_path, monkeypatch)

    def reject_changed_target(_runner: object, upstream: object) -> None:
        target = (
            "installed" if upstream == harness.installed_route.upstream else "current"
        )
        stage = harness._stage()
        harness.events.append(f"identity:{target}:{stage}")
        if target == "installed" and stage == "old":
            raise module.DeploymentError("target identity changed")

    monkeypatch.setattr(
        module,
        "_assert_upstream_identity_current",
        reject_changed_target,
    )

    with pytest.raises(module.DeploymentError, match="identity changed"):
        invoke_install(module, harness)

    assert harness.events == ["healthy:installed:old", "identity:installed:old"]
    assert harness.current_fragment == harness.old_fragment
    assert harness.active_head is harness.parent


def test_installed_site_bytes_are_checked_before_caddy_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    install_harness(module, harness, tmp_path, monkeypatch)
    original_install = harness.install

    def install_then_tamper(
        runner: object,
        content: str,
        *,
        expected_sha256: str,
        expected_current_sha256: str,
    ) -> None:
        original_install(
            runner,
            content,
            expected_sha256=expected_sha256,
            expected_current_sha256=expected_current_sha256,
        )
        harness.current_fragment = harness.new_fragment + "# injected\n"

    monkeypatch.setattr(module, "_atomic_install_site", install_then_tamper)

    with pytest.raises(module.DeploymentError, match="bytes|digest|outside"):
        invoke_install(module, harness)

    assert harness.events == [
        "healthy:installed:old",
        "upstream:installed:old",
        "persist",
        "install:new",
        "observe-head",
    ]
    assert "validate:new" not in harness.events


def test_commit_failure_before_head_publication_restores_and_keeps_the_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    harness.commit_outcome = "before"
    install_harness(module, harness, tmp_path, monkeypatch)

    with pytest.raises(module.DeploymentError, match="pre-publish commit"):
        invoke_install(module, harness)

    assert harness.events == SUCCESS_EVENTS + ["observe-head"] + RECOVERY_EVENTS
    assert harness.current_fragment == harness.old_fragment
    assert harness.active_head is harness.parent


def test_commit_timeout_refuses_automatic_restoration_while_child_may_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    harness.commit_outcome = "timeout"
    install_harness(module, harness, tmp_path, monkeypatch)

    with pytest.raises(
        module.DeploymentError,
        match="timeout|indeterminate|restoration refused",
    ):
        invoke_install(module, harness)

    assert harness.events == SUCCESS_EVENTS
    assert harness.current_fragment == harness.new_fragment
    assert harness.active_head is harness.parent
    assert "install:old" not in harness.events


def test_indeterminate_mutator_error_refuses_restoration_without_timeout_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()

    class SyntheticIndeterminateMutation(module.DeploymentError):
        pass

    monkeypatch.setattr(
        module,
        "IndeterminateCaddyMutationError",
        SyntheticIndeterminateMutation,
        raising=False,
    )
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    harness.commit_outcome = "indeterminate-timeout"
    install_harness(module, harness, tmp_path, monkeypatch)

    with pytest.raises(
        module.DeploymentError, match="restoration refused|indeterminate"
    ):
        invoke_install(module, harness)

    assert harness.events == SUCCESS_EVENTS
    assert harness.current_fragment == harness.new_fragment
    assert harness.active_head is harness.parent
    assert "install:old" not in harness.events


def test_precommit_failure_refuses_restoration_after_external_head_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    install_harness(module, harness, tmp_path, monkeypatch)
    foreign_active_text = '{"transaction":"foreign"}\n'

    def drift_then_fail(_runner: object) -> None:
        harness.events.append("validate:new")
        harness.active_head = replace(
            harness.new_head,
            active_text=foreign_active_text,
            active_sha256=hashlib.sha256(
                foreign_active_text.encode("utf-8")
            ).hexdigest(),
            transaction_id="20261009T130000Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        raise module.DeploymentError("injected validation failure")

    monkeypatch.setattr(module, "_validate_caddy", drift_then_fail)

    with pytest.raises(module.DeploymentError, match="outside|drift"):
        invoke_install(module, harness)

    assert harness.events == [
        "healthy:installed:old",
        "upstream:installed:old",
        "persist",
        "install:new",
        "validate:new",
        "observe-head",
    ]
    assert harness.current_fragment == harness.new_fragment
    assert "install:old" not in harness.events


def test_restoration_failure_is_reported_with_the_trusted_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    harness.fail_stage = "validate"
    install_harness(module, harness, tmp_path, monkeypatch)

    def fail_restoration_reload(_runner: object) -> None:
        harness.events.append(f"reload:{harness._stage()}")
        if harness._stage() == "old":
            raise module.DeploymentError("injected restoration reload failure")

    monkeypatch.setattr(module, "_reload_caddy", fail_restoration_reload)

    with pytest.raises(
        module.DeploymentError,
        match="automatic restoration also failed.*trusted backup",
    ):
        invoke_install(module, harness)

    assert harness.current_fragment == harness.old_fragment
    assert harness.active_head is harness.parent
    assert harness.events[-3:] == ["install:old", "validate:old", "reload:old"]


@pytest.mark.parametrize("with_parent", [False, True], ids=("bootstrap", "chained"))
def test_visible_new_head_without_durability_ack_is_indeterminate_without_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_parent: bool,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=with_parent)
    harness.commit_outcome = "after"
    install_harness(module, harness, tmp_path, monkeypatch)

    with pytest.raises(module.DeploymentError, match="durability|indeterminate"):
        invoke_install(module, harness)

    assert harness.events == SUCCESS_EVENTS + ["observe-head"]
    assert harness.current_fragment == harness.new_fragment
    assert harness.active_head is harness.new_head
    assert "install:old" not in harness.events


def test_same_transaction_id_with_different_ledger_is_external_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    harness = TransactionV3Harness(module, tmp_path, with_parent=True)
    harness.commit_outcome = "after-forged"
    install_harness(module, harness, tmp_path, monkeypatch)

    with pytest.raises(module.DeploymentError, match="outside|indeterminate|drift"):
        invoke_install(module, harness)

    assert harness.events == SUCCESS_EVENTS + ["observe-head"]
    assert harness.current_fragment == harness.new_fragment
    assert "install:old" not in harness.events
