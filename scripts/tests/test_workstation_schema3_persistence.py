"""Persistence and linearization contracts for schema-3 Caddy transactions."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
BOOTSTRAP_ID = "b" * 64
PARENT_TRANSACTION_ID = "20261009T110000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TRANSACTION_ID = "20261009T120000Z-0123456789abcdef0123456789abcdef"
DEPLOYMENT_ASSET_PATHS = (
    "deploy/workstation/compose.yaml",
    "deploy/workstation/commerce-ops-desk.Caddyfile.template",
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template",
)


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_schema3_persistence", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stable_json(payload: object) -> str:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deployment_assets_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "sha256": {
            DEPLOYMENT_ASSET_PATHS[0]: "6" * 64,
            DEPLOYMENT_ASSET_PATHS[1]: "7" * 64,
            DEPLOYMENT_ASSET_PATHS[2]: "8" * 64,
        },
    }


def upstream_payload(*, host_port: int) -> dict[str, object]:
    return {
        "schema": 1,
        "docker_daemon_id": "local-daemon-id",
        "container_id": "1" * 64,
        "container_name": "app-commerce-ops-desk-candidate-0123456789ab",
        "image_id": "sha256:" + "2" * 64,
        "image_reference": f"commerce-ops-desk:{SOURCE_SHA}",
        "source_sha": SOURCE_SHA,
        "host_port": host_port,
        "data_path": (
            "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab"
        ),
        "data_device": 64_769,
        "data_inode": 55_451_027,
        "network_name": "commerce-ops-candidate-0123456789ab_default",
        "network_id": "3" * 64,
        "network_endpoint_id": "4" * 64,
        "runtime_sha256": "5" * 64,
    }


def route_states(module: ModuleType) -> tuple[object, object]:
    backup_upstream = module._validate_upstream_identity_payload(
        upstream_payload(host_port=18_087)
    )
    installed_upstream = module._validate_upstream_identity_payload(
        upstream_payload(host_port=18_088)
    )
    assets = deployment_assets_payload()
    backup = module.RouteState(
        fragment_sha256=sha256_text("legacy fragment\n"),
        profile=module.CADDY_PROFILE_LEGACY_V020,
        route_revision=None,
        upstream=backup_upstream,
        deployment_assets=None,
    )
    installed = module.RouteState(
        fragment_sha256=sha256_text("hardened fragment\n"),
        profile=module.CADDY_PROFILE_HARDENED,
        route_revision=module._route_revision(
            module.CADDY_PROFILE_HARDENED,
            installed_upstream,
            assets,
        ),
        upstream=installed_upstream,
        deployment_assets=MappingProxyType(assets),
    )
    return backup, installed


class RootFilesystemRunner:
    """In-memory root filesystem; it records sudo argv but executes no process."""

    def __init__(self) -> None:
        self.directories: set[Path] = set()
        self.files: dict[Path, str] = {}
        self.calls: list[list[str]] = []
        self.events: list[str] = []

    def seed_directory(self, path: Path) -> None:
        self.directories.add(path)

    def seed_file(self, path: Path, text: str) -> None:
        self.directories.add(path.parent)
        self.files[path] = text

    def run(
        self,
        arguments: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append(command)
        self.events.append(f"runner:{command[1]}")
        assert command[0] == "/usr/bin/sudo", (
            "the unit test runner must never execute an unmodelled command"
        )
        tool = command[1]
        if tool == "install" and "-d" in command:
            self.directories.add(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "", "")
        if tool == "install":
            source = Path(command[-2])
            target = Path(command[-1])
            self.directories.add(target.parent)
            self.files[target] = source.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        if tool == "ln":
            source = Path(command[-2])
            target = Path(command[-1])
            if target in self.files:
                raise AssertionError("immutable transaction target already exists")
            self.files[target] = self.files[source]
            return subprocess.CompletedProcess(command, 0, "", "")
        if tool == "/usr/bin/python3":
            assert command[2:4] == ["-I", "-c"]
            if "transaction directory hierarchy is invalid" in command[4]:
                anchor, parent, target = map(Path, command[-3:])
                assert parent.parent == anchor
                assert target.parent == parent
                self.directories.update({anchor, parent, target})
                assert "os.fsync" in command[4]
                return subprocess.CompletedProcess(command, 0, "", "")
            target = Path(command[-2])
            directory = Path(command[-1])
            assert target in self.files
            assert target.parent == directory
            assert directory in self.directories
            assert "os.fsync" in command[4]
            return subprocess.CompletedProcess(command, 0, "", "")
        if tool == "rm":
            self.files.pop(Path(command[-1]), None)
            return subprocess.CompletedProcess(command, 0, "", "")
        if tool == "stat":
            path = Path(command[-1])
            if path in self.directories:
                mode = stat.S_IFDIR | 0o700
            elif path in self.files:
                mode = stat.S_IFREG | 0o600
            else:
                return subprocess.CompletedProcess(command, 1, "", "missing")
            return subprocess.CompletedProcess(
                command,
                0,
                f"{mode:x}|0|0|1\n",
                "",
            )
        if tool == "cat":
            path = Path(command[-1])
            if path not in self.files:
                return subprocess.CompletedProcess(command, 1, "", "missing")
            return subprocess.CompletedProcess(command, 0, self.files[path], "")
        if tool == "test" and command[2:3] == ["-f"]:
            return subprocess.CompletedProcess(
                command,
                0 if Path(command[-1]) in self.files else 1,
                "",
                "",
            )
        if tool == "test" and command[2:3] == ["-L"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        raise AssertionError(f"unexpected fake-root command: {command}")


def configure_paths(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    state_directory = tmp_path / "deploy-state"
    transaction_directory = tmp_path / "root" / "caddy-transactions"
    active_path = transaction_directory / "active.json"
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(
        module,
        "CADDY_TRANSACTION_DIRECTORY",
        transaction_directory,
    )
    monkeypatch.setattr(module, "CADDY_ACTIVE_STATE", active_path, raising=False)
    return transaction_directory, active_path


def transaction_ledger_payload(
    module: ModuleType,
    *,
    transaction_id: str,
    bootstrap_id: str,
    operation: str,
    parent_transaction_id: str | None,
    parent_active_sha256: str | None,
    backup_path: Path,
    backup: object,
    installed: object,
) -> dict[str, object]:
    backup_payload = module._route_state_payload(backup)
    installed_payload = module._route_state_payload(installed)
    return {
        "schema": 3,
        "transaction_id": transaction_id,
        "operation": operation,
        "bootstrap_id": bootstrap_id,
        "parent": {
            "transaction_id": parent_transaction_id,
            "active_sha256": parent_active_sha256,
        },
        "site": {
            "path": str(module.CADDY_SITE),
            "host": module.CADDY_HOST,
        },
        "backup": {"path": str(backup_path), **backup_payload},
        "installed": installed_payload,
    }


def persisted_transaction(
    module: ModuleType,
    transaction_directory: Path,
    *,
    transaction_id: str = TRANSACTION_ID,
    bootstrap_id: str = BOOTSTRAP_ID,
    operation: str = "switch",
    parent_transaction_id: str | None = None,
    parent_active_sha256: str | None = None,
    backup: object | None = None,
    installed: object | None = None,
) -> object:
    default_backup, default_installed = route_states(module)
    backup = default_backup if backup is None else backup
    installed = default_installed if installed is None else installed
    backup_path = transaction_directory / (f"commerce-ops-desk.{transaction_id}.conf")
    ledger_path = backup_path.with_suffix(".json")
    payload = transaction_ledger_payload(
        module,
        transaction_id=transaction_id,
        bootstrap_id=bootstrap_id,
        operation=operation,
        parent_transaction_id=parent_transaction_id,
        parent_active_sha256=parent_active_sha256,
        backup_path=backup_path,
        backup=backup,
        installed=installed,
    )
    ledger_text = stable_json(payload)
    return module.PersistedCaddyTransaction(
        transaction_id=transaction_id,
        operation=operation,
        bootstrap_id=bootstrap_id,
        backup_path=backup_path,
        ledger_path=ledger_path,
        ledger_text=ledger_text,
        ledger_sha256=sha256_text(ledger_text),
        backup=backup,
        installed=installed,
    )


def active_payload(module: ModuleType, transaction: object) -> dict[str, object]:
    return {
        "schema": 1,
        "bootstrap_id": transaction.bootstrap_id,
        "transaction_id": transaction.transaction_id,
        "ledger_path": str(transaction.ledger_path),
        "ledger_sha256": transaction.ledger_sha256,
        "site": {
            "path": str(module.CADDY_SITE),
            "host": module.CADDY_HOST,
        },
        "installed": module._route_state_payload(transaction.installed),
    }


def active_chain(module: ModuleType, transaction: object) -> object:
    active_text = stable_json(active_payload(module, transaction))
    return module.ActiveCaddyChain(
        active_text=active_text,
        active_sha256=sha256_text(active_text),
        bootstrap_id=transaction.bootstrap_id,
        transaction_id=transaction.transaction_id,
        installed=transaction.installed,
        transaction=transaction,
    )


def seed_transaction(
    runner: RootFilesystemRunner,
    transaction: object,
    *,
    backup_text: str | None = None,
) -> None:
    if backup_text is None:
        backup_text = next(
            candidate
            for candidate in ("legacy fragment\n", "hardened fragment\n")
            if sha256_text(candidate) == transaction.backup.fragment_sha256
        )
    runner.seed_file(transaction.backup_path, backup_text)
    runner.seed_file(transaction.ledger_path, transaction.ledger_text)


def test_persisted_transaction_has_only_the_complete_schema3_identity() -> None:
    module = load_deploy_tool()

    assert [
        field.name for field in dataclasses.fields(module.PersistedCaddyTransaction)
    ] == [
        "transaction_id",
        "operation",
        "bootstrap_id",
        "backup_path",
        "ledger_path",
        "ledger_text",
        "ledger_sha256",
        "backup",
        "installed",
    ]


def test_bootstrap_persistence_writes_root_owned_stable_schema3_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, _ = configure_paths(module, monkeypatch, tmp_path)
    backup, installed = route_states(module)
    runner = RootFilesystemRunner()
    original = "legacy fragment\n"

    transaction = module._persist_caddy_transaction(
        runner,
        original,
        operation="switch",
        parent=None,
        backup=backup,
        installed=installed,
    )

    assert isinstance(transaction, module.PersistedCaddyTransaction)
    assert transaction.operation == "switch"
    assert transaction.backup == backup
    assert transaction.installed == installed
    assert transaction.backup_path.parent == transaction_directory
    assert transaction.ledger_path == transaction.backup_path.with_suffix(".json")
    assert runner.files[transaction.backup_path] == original
    assert runner.files[transaction.ledger_path] == transaction.ledger_text
    payload = json.loads(transaction.ledger_text)
    assert payload == transaction_ledger_payload(
        module,
        transaction_id=transaction.transaction_id,
        bootstrap_id=transaction.bootstrap_id,
        operation="switch",
        parent_transaction_id=None,
        parent_active_sha256=None,
        backup_path=transaction.backup_path,
        backup=backup,
        installed=installed,
    )
    assert transaction.ledger_text == stable_json(payload)
    assert transaction.ledger_text.endswith("\n")
    assert transaction.ledger_sha256 == sha256_text(transaction.ledger_text)
    assert all(command[0] == "/usr/bin/sudo" for command in runner.calls)
    installs = [
        command
        for command in runner.calls
        if command[1] == "install" and "-d" not in command
    ]
    assert len(installs) == 2
    assert all(
        command[2:8] == ["-o", "root", "-g", "root", "-m", "0600"]
        for command in installs
    )
    assert all(Path(command[-1]).name.startswith(".immutable-") for command in installs)
    links = [command for command in runner.calls if command[1] == "ln"]
    assert [Path(command[-1]) for command in links] == [
        transaction.backup_path,
        transaction.ledger_path,
    ]


def test_chained_persistence_inherits_bootstrap_and_binds_exact_parent_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, _ = configure_paths(module, monkeypatch, tmp_path)
    _, installed = route_states(module)
    parent_transaction = persisted_transaction(
        module,
        transaction_directory,
        transaction_id=PARENT_TRANSACTION_ID,
    )
    parent = active_chain(module, parent_transaction)
    runner = RootFilesystemRunner()

    transaction = module._persist_caddy_transaction(
        runner,
        "hardened fragment\n",
        operation="switch",
        parent=parent,
        backup=installed,
        installed=installed,
    )

    payload = json.loads(transaction.ledger_text)
    assert transaction.bootstrap_id == parent.bootstrap_id
    assert payload["bootstrap_id"] == parent.bootstrap_id
    assert payload["parent"] == {
        "transaction_id": parent.transaction_id,
        "active_sha256": parent.active_sha256,
    }
    assert transaction.ledger_sha256 == sha256_text(transaction.ledger_text)


def test_persistence_rejects_a_noncontinuous_parent_or_bootstrap_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, _ = configure_paths(module, monkeypatch, tmp_path)
    backup, installed = route_states(module)
    inconsistent_parent = active_chain(
        module,
        persisted_transaction(
            module,
            transaction_directory,
            transaction_id=PARENT_TRANSACTION_ID,
        ),
    )

    with pytest.raises(module.DeploymentError, match="parent|continuous"):
        module._persist_caddy_transaction(
            RootFilesystemRunner(),
            "legacy fragment\n",
            operation="switch",
            parent=inconsistent_parent,
            backup=backup,
            installed=installed,
        )

    with pytest.raises(module.DeploymentError, match="parent|bootstrap"):
        module._persist_caddy_transaction(
            RootFilesystemRunner(),
            "legacy fragment\n",
            operation="rollback",
            parent=None,
            backup=backup,
            installed=backup,
        )


def test_rollback_persistence_requires_the_parent_backup_as_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, _ = configure_paths(module, monkeypatch, tmp_path)
    _, current_route = route_states(module)
    parent_transaction = persisted_transaction(
        module,
        transaction_directory,
        transaction_id=PARENT_TRANSACTION_ID,
    )
    parent = active_chain(module, parent_transaction)

    with pytest.raises(module.DeploymentError, match="rollback.*target|parent.*backup"):
        module._persist_caddy_transaction(
            RootFilesystemRunner(),
            "hardened fragment\n",
            operation="rollback",
            parent=parent,
            backup=current_route,
            installed=current_route,
        )


def install_atomic_fault(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    runner: RootFilesystemRunner,
    *,
    active_path: Path,
    mode: str,
    drift_text: str | None = None,
) -> None:
    def atomic_install(
        received_runner: object,
        path: Path,
        text: str,
        *,
        expected_sha256: str | None,
    ) -> None:
        assert received_runner is runner
        assert path == active_path
        current = runner.files.get(path)
        current_sha256 = None if current is None else sha256_text(current)
        assert current_sha256 == expected_sha256
        runner.events.append("atomic-install")
        if mode == "before-replace":
            raise module.DeploymentError("injected before replace")
        if mode == "timeout-before-replace":
            error = module.DeploymentError("injected active commit timeout")
            error.__cause__ = subprocess.TimeoutExpired(cmd="sudo", timeout=60)
            raise error
        runner.seed_file(path, text)
        if mode == "after-replace":
            raise module.DeploymentError("injected after replace")
        if mode == "external-drift":
            assert drift_text is not None
            runner.seed_file(path, drift_text)
            raise module.DeploymentError("compare-and-swap rejected external drift")

    monkeypatch.setattr(
        module,
        "_atomic_install_root_owned_text",
        atomic_install,
        raising=False,
    )


def test_active_commit_derives_schema1_head_and_atomic_install_is_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    transaction = persisted_transaction(module, transaction_directory)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, transaction)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="success",
    )

    chain = module._commit_active_caddy_state(
        runner,
        transaction,
        expected_parent=None,
    )

    expected_text = stable_json(active_payload(module, transaction))
    assert runner.files[active_path] == expected_text
    assert chain == module.ActiveCaddyChain(
        active_text=expected_text,
        active_sha256=sha256_text(expected_text),
        bootstrap_id=transaction.bootstrap_id,
        transaction_id=transaction.transaction_id,
        installed=transaction.installed,
        transaction=transaction,
    )
    assert runner.events[-1] == "atomic-install"


def test_active_atomic_helper_rechecks_the_opened_parent_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    del transaction_directory

    class CaptureRunner:
        def __init__(self) -> None:
            self.command: list[str] | None = None
            self.input_text: str | None = None

        def run(
            self,
            arguments: Sequence[str],
            *,
            input_text: str | None = None,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            self.command = list(arguments)
            self.input_text = input_text
            return subprocess.CompletedProcess(self.command, 0, "", "")

    runner = CaptureRunner()
    runner._caddy_mutation_fence_token = "a" * 64
    module._atomic_install_root_owned_text(
        runner,
        active_path,
        "{}\n",
        expected_sha256=None,
    )

    assert runner.command is not None
    helper = runner.command[4]
    compile(helper, "<active-state-helper>", "exec")
    assert "os.fstat" in helper
    assert "st_dev" in helper
    assert "st_ino" in helper
    assert "fcntl.flock" in helper
    assert "fence token" in helper
    assert runner.input_text == "{}\n"


def test_durable_active_verifier_fsyncs_and_rechecks_exact_root_owned_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    _, active_path = configure_paths(module, monkeypatch, tmp_path)

    class CaptureRunner:
        def __init__(self) -> None:
            self.command: list[str] | None = None
            self.input_text: str | None = None

        def run(
            self,
            arguments: Sequence[str],
            *,
            input_text: str | None = None,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            self.command = list(arguments)
            self.input_text = input_text
            return subprocess.CompletedProcess(self.command, 0, "", "")

    runner = CaptureRunner()
    runner._caddy_mutation_fence_token = "a" * 64
    expected = '{"schema":1}\n'

    module._verify_durable_root_owned_text(runner, active_path, expected)

    assert runner.command is not None
    command = runner.command
    assert command[:4] == [
        module.SUDO_BINARY,
        module.PYTHON_BINARY,
        "-I",
        "-c",
    ]
    helper = command[4]
    compile(helper, "<durable-active-state-verifier>", "exec")
    assert helper.count("os.fsync") >= 2
    assert "O_NOFOLLOW" in helper
    assert "os.lstat" in helper
    assert "os.fstat" in helper
    assert "st_nlink" in helper
    assert "hashlib.sha256" in helper
    assert "os.lseek" in helper
    assert "fcntl.flock" in helper
    assert "fence token" in helper
    assert command[-5:-3] == [str(active_path), str(active_path.parent)]
    assert command[-2:] == [
        str(module.CADDY_MUTATION_LOCK),
        str(module.CADDY_MUTATION_FENCE),
    ]
    assert runner.input_text == expected


def parent_fixture(
    module: ModuleType,
    transaction_directory: Path,
) -> tuple[object, object]:
    _, installed = route_states(module)
    parent_transaction = persisted_transaction(
        module,
        transaction_directory,
        transaction_id=PARENT_TRANSACTION_ID,
    )
    parent = active_chain(module, parent_transaction)
    transaction = persisted_transaction(
        module,
        transaction_directory,
        parent_transaction_id=parent.transaction_id,
        parent_active_sha256=parent.active_sha256,
        backup=installed,
    )
    return parent, transaction


def test_commit_failure_before_replace_leaves_parent_and_reports_not_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, parent.transaction)
    seed_transaction(runner, transaction)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="before-replace",
    )

    with pytest.raises(module.DeploymentError, match="before replace|not committed"):
        module._commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )

    assert runner.files[active_path] == parent.active_text
    assert runner.events.count("atomic-install") == 1


def test_commit_timeout_with_parent_visible_remains_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, parent.transaction)
    seed_transaction(runner, transaction)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="timeout-before-replace",
    )

    with pytest.raises(module.DeploymentError, match="indeterminate|timeout"):
        module._commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )

    assert runner.files[active_path] == parent.active_text
    assert runner.events.count("atomic-install") == 1


def test_commit_timeout_preserves_indeterminate_type_when_observation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, parent.transaction)
    seed_transaction(runner, transaction)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="timeout-before-replace",
    )

    def fail_observation(*_args: object, **_kwargs: object) -> object:
        raise module.DeploymentError("injected observation failure")

    monkeypatch.setattr(
        module,
        "_load_active_caddy_chain_with_fragment_policy",
        fail_observation,
    )

    with pytest.raises(module.DeploymentError) as captured:
        module._commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )

    assert type(captured.value).__name__ == "IndeterminateCaddyMutationError"


def test_commit_failure_after_replace_returns_the_visible_new_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, parent.transaction)
    seed_transaction(runner, transaction)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="after-replace",
    )
    durable_verifications: list[str] = []

    def verify_durable(
        received_runner: object,
        path: Path,
        text: str,
    ) -> None:
        assert received_runner is runner
        assert path == active_path
        durable_verifications.append(text)

    monkeypatch.setattr(
        module,
        "_verify_durable_root_owned_text",
        verify_durable,
        raising=False,
    )

    chain = module._commit_active_caddy_state(
        runner,
        transaction,
        expected_parent=parent,
    )

    expected_text = stable_json(active_payload(module, transaction))
    assert runner.files[active_path] == expected_text
    assert chain.active_text == expected_text
    assert chain.active_sha256 == sha256_text(expected_text)
    assert chain.transaction is transaction
    assert runner.events.count("atomic-install") == 1
    assert durable_verifications == [expected_text]


def test_visible_new_head_is_not_acknowledged_when_durability_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, parent.transaction)
    seed_transaction(runner, transaction)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="after-replace",
    )

    def reject_durability(*_args: object) -> None:
        raise module.DeploymentError("injected directory fsync failure")

    monkeypatch.setattr(
        module,
        "_verify_durable_root_owned_text",
        reject_durability,
        raising=False,
    )

    with pytest.raises(module.DeploymentError, match="durability|indeterminate"):
        module._commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )

    assert runner.files[active_path] == stable_json(active_payload(module, transaction))


def test_durable_new_head_is_reloaded_before_commit_is_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    proposed = active_chain(module, transaction)
    external_transaction = persisted_transaction(
        module,
        transaction_directory,
        transaction_id="20261009T130000Z-cccccccccccccccccccccccccccccccc",
        parent_transaction_id=transaction.transaction_id,
        parent_active_sha256=proposed.active_sha256,
        backup=transaction.installed,
        installed=transaction.installed,
    )
    external = active_chain(module, external_transaction)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    for persisted in (parent.transaction, transaction, external_transaction):
        seed_transaction(runner, persisted)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="after-replace",
    )

    def drift_after_durability(*_args: object) -> None:
        runner.seed_file(active_path, external.active_text)

    monkeypatch.setattr(
        module,
        "_verify_durable_root_owned_text",
        drift_after_durability,
    )

    with pytest.raises(module.DeploymentError, match="changed|indeterminate"):
        module._commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )

    assert runner.files[active_path] == external.active_text


def test_commit_failure_with_external_drift_fails_closed_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory, active_path = configure_paths(module, monkeypatch, tmp_path)
    parent, transaction = parent_fixture(module, transaction_directory)
    external_transaction = persisted_transaction(
        module,
        transaction_directory,
        transaction_id="20261009T130000Z-cccccccccccccccccccccccccccccccc",
        parent_transaction_id=parent.transaction_id,
        parent_active_sha256=parent.active_sha256,
    )
    external = active_chain(module, external_transaction)
    runner = RootFilesystemRunner()
    runner.seed_directory(transaction_directory)
    seed_transaction(runner, parent.transaction)
    seed_transaction(runner, transaction)
    seed_transaction(runner, external_transaction)
    runner.seed_file(active_path, parent.active_text)
    install_atomic_fault(
        module,
        monkeypatch,
        runner,
        active_path=active_path,
        mode="external-drift",
        drift_text=external.active_text,
    )

    with pytest.raises(module.DeploymentError, match="drift|indeterminate"):
        module._commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )

    assert runner.files[active_path] == external.active_text
    assert runner.events.count("atomic-install") == 1
