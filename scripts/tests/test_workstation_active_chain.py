"""Linear active-head contracts for workstation Caddy transactions."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
BOOTSTRAP_ID = "b" * 64
T1_ID = "20261009T120000Z-11111111111111111111111111111111"
T2_ID = "20261009T121000Z-22222222222222222222222222222222"
OTHER_ID = "20261009T122000Z-33333333333333333333333333333333"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_active_chain", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def json_record(payload: object) -> str:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deployment_assets(module: ModuleType, seed: str) -> dict[str, object]:
    return {
        "schema": module.DEPLOYMENT_ASSET_SCHEMA,
        "sha256": {path: seed * 64 for path in module.DEPLOYMENT_ASSET_PATHS},
    }


def upstream_payload(
    module: ModuleType,
    *,
    seed: str,
    source_seed: str,
    port: int,
) -> dict[str, object]:
    return {
        "schema": module.UPSTREAM_IDENTITY_SCHEMA,
        "docker_daemon_id": f"daemon-{seed}",
        "container_id": seed * 64,
        "container_name": f"commerce-ops-{seed}",
        "image_id": "sha256:" + seed * 64,
        "image_reference": f"commerce-ops-desk:{source_seed * 40}",
        "source_sha": source_seed * 40,
        "host_port": port,
        "data_path": f"/srv/commerce-ops-desk/data-{seed}",
        "data_device": 64_769,
        "data_inode": 55_451_000 + port,
        "network_name": f"commerce-ops-{seed}_default",
        "network_id": seed * 64,
        "network_endpoint_id": source_seed * 64,
        "runtime_sha256": source_seed * 64,
    }


def route_state(
    module: ModuleType,
    *,
    fragment: str,
    profile: str,
    upstream: dict[str, object],
    asset_seed: str | None,
) -> dict[str, object]:
    if asset_seed is None:
        assets = None
        revision = None
    else:
        assets = deployment_assets(module, asset_seed)
        revision = module._route_revision(
            profile,
            module._validate_upstream_identity_payload(upstream),
            assets,
        )
    return {
        "fragment_sha256": text_sha256(fragment),
        "profile": profile,
        "route_revision": revision,
        "upstream": upstream,
        "deployment_assets": assets,
    }


@dataclass
class ChainFixture:
    root: Path
    active_path: Path
    legacy_fragment: str
    hardened_fragment: str
    legacy_route: dict[str, object]
    hardened_route: dict[str, object]
    t1_backup: Path
    t1_ledger: Path
    t1_payload: dict[str, object]
    t1_text: str
    h1_payload: dict[str, object]
    h1_text: str
    t2_backup: Path
    t2_ledger: Path
    t2_payload: dict[str, object]
    t2_text: str
    h2_payload: dict[str, object]
    h2_text: str
    files: dict[Path, str]

    def publish_h1(self) -> None:
        self.files[self.active_path] = self.h1_text

    def publish_h2(self) -> None:
        self.files[self.active_path] = self.h2_text


def build_chain(module: ModuleType, root: Path) -> ChainFixture:
    active_path = root / "active.json"
    legacy_template = module.LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8")
    legacy_fragment = module.render_caddy_template(legacy_template, 18_087)
    legacy_upstream = upstream_payload(
        module,
        seed="1",
        source_seed="2",
        port=18_087,
    )
    legacy_route = route_state(
        module,
        fragment=legacy_fragment,
        profile=module.CADDY_PROFILE_LEGACY_V020,
        upstream=legacy_upstream,
        asset_seed=None,
    )

    hardened_upstream = upstream_payload(
        module,
        seed="3",
        source_seed="4",
        port=18_088,
    )
    hardened_assets = deployment_assets(module, "5")
    hardened_revision = module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        module._validate_upstream_identity_payload(hardened_upstream),
        hardened_assets,
    )
    hardened_fragment = module.render_caddy_template(
        module.CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18_088,
        route_revision=hardened_revision,
    )
    hardened_route = {
        "fragment_sha256": text_sha256(hardened_fragment),
        "profile": module.CADDY_PROFILE_HARDENED,
        "route_revision": hardened_revision,
        "upstream": hardened_upstream,
        "deployment_assets": hardened_assets,
    }

    site = {"path": str(module.CADDY_SITE), "host": module.CADDY_HOST}
    t1_backup = root / f"commerce-ops-desk.{T1_ID}.conf"
    t1_ledger = t1_backup.with_suffix(".json")
    t1_payload: dict[str, object] = {
        "schema": module.CADDY_TRANSACTION_SCHEMA,
        "transaction_id": T1_ID,
        "operation": "switch",
        "bootstrap_id": BOOTSTRAP_ID,
        "parent": {"transaction_id": None, "active_sha256": None},
        "site": site,
        "backup": {"path": str(t1_backup), **legacy_route},
        "installed": hardened_route,
    }
    t1_text = json_record(t1_payload)
    h1_payload: dict[str, object] = {
        "schema": module.ACTIVE_STATE_SCHEMA,
        "bootstrap_id": BOOTSTRAP_ID,
        "transaction_id": T1_ID,
        "ledger_path": str(t1_ledger),
        "ledger_sha256": text_sha256(t1_text),
        "site": site,
        "installed": hardened_route,
    }
    h1_text = json_record(h1_payload)

    t2_backup = root / f"commerce-ops-desk.{T2_ID}.conf"
    t2_ledger = t2_backup.with_suffix(".json")
    t2_payload: dict[str, object] = {
        "schema": module.CADDY_TRANSACTION_SCHEMA,
        "transaction_id": T2_ID,
        "operation": "rollback",
        "bootstrap_id": BOOTSTRAP_ID,
        "parent": {
            "transaction_id": T1_ID,
            "active_sha256": text_sha256(h1_text),
        },
        "site": site,
        "backup": {"path": str(t2_backup), **hardened_route},
        "installed": legacy_route,
    }
    t2_text = json_record(t2_payload)
    h2_payload: dict[str, object] = {
        "schema": module.ACTIVE_STATE_SCHEMA,
        "bootstrap_id": BOOTSTRAP_ID,
        "transaction_id": T2_ID,
        "ledger_path": str(t2_ledger),
        "ledger_sha256": text_sha256(t2_text),
        "site": site,
        "installed": legacy_route,
    }
    h2_text = json_record(h2_payload)
    files = {
        t1_backup: legacy_fragment,
        t1_ledger: t1_text,
        t2_backup: hardened_fragment,
        t2_ledger: t2_text,
        active_path: h1_text,
    }
    return ChainFixture(
        root=root,
        active_path=active_path,
        legacy_fragment=legacy_fragment,
        hardened_fragment=hardened_fragment,
        legacy_route=legacy_route,
        hardened_route=hardened_route,
        t1_backup=t1_backup,
        t1_ledger=t1_ledger,
        t1_payload=t1_payload,
        t1_text=t1_text,
        h1_payload=h1_payload,
        h1_text=h1_text,
        t2_backup=t2_backup,
        t2_ledger=t2_ledger,
        t2_payload=t2_payload,
        t2_text=t2_text,
        h2_payload=h2_payload,
        h2_text=h2_text,
        files=files,
    )


class RootFilesRunner:
    """Expose root-owned archive bytes without touching the host filesystem."""

    def __init__(self, module: ModuleType, fixture: ChainFixture) -> None:
        self.module = module
        self.fixture = fixture
        self.commands: list[list[str]] = []
        self.mutating_commands: list[list[str]] = []

    def reset_commands(self) -> None:
        self.commands.clear()
        self.mutating_commands.clear()

    def run(
        self,
        arguments: list[str] | tuple[str, ...],
        *,
        input_text: str | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.commands.append(command)
        if command[:2] == [self.module.CADDY_BINARY, "adapt"]:
            assert input_text is not None
            port_match = re.search(r"127\.0\.0\.1:([0-9]{1,5})", input_text)
            assert port_match is not None
            revision_match = re.search(
                r'X-CommerceOps-Route-Revision\s+"?([0-9a-f]{64})"?',
                input_text,
            )
            document = {
                "profile": (
                    self.module.CADDY_PROFILE_HARDENED
                    if revision_match is not None
                    else self.module.CADDY_PROFILE_LEGACY_V020
                ),
                "port": int(port_match.group(1)),
                "revision": None if revision_match is None else revision_match.group(1),
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(document), "")

        if not command or command[0] != self.module.SUDO_BINARY:
            raise AssertionError(f"unexpected command: {command!r}")
        if command[1:3] == ["stat", "--format=%f|%u|%g|%h"]:
            path = Path(command[-1])
            if path == self.fixture.root:
                output = "41c0|0|0|2\n"
            elif path in self.fixture.files:
                output = "8180|0|0|1\n"
            else:
                raise AssertionError(f"unexpected stat path: {path}")
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[1] == "cat":
            path = Path(command[-1])
            return subprocess.CompletedProcess(
                command,
                0,
                self.fixture.files[path],
                "",
            )
        if command[1] == "test":
            predicate = command[2]
            path = Path(command[-1])
            exists = path == self.fixture.root or path in self.fixture.files
            if predicate == "-L":
                return subprocess.CompletedProcess(command, 1, "", "")
            if predicate in {"-e", "-f"}:
                return subprocess.CompletedProcess(command, 0 if exists else 1, "", "")
            if predicate == "-d":
                return subprocess.CompletedProcess(
                    command,
                    0 if path == self.fixture.root else 1,
                    "",
                    "",
                )

        self.mutating_commands.append(command)
        raise AssertionError(f"active-chain loading attempted a write: {command!r}")

    def cat_paths(self) -> list[Path]:
        return [
            Path(command[-1])
            for command in self.commands
            if len(command) > 1
            and command[0] == self.module.SUDO_BINARY
            and command[1] == "cat"
        ]


def install_paths(
    module: ModuleType,
    fixture: ChainFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "CADDY_TRANSACTION_DIRECTORY", fixture.root)
    monkeypatch.setattr(
        module,
        "CADDY_ACTIVE_STATE",
        fixture.active_path,
        raising=False,
    )


def assert_route_payload(module: ModuleType, route: object, expected: object) -> None:
    assert isinstance(route, module.RouteState)
    assert module._route_state_payload(route) == expected


def test_active_chain_loads_one_fully_bound_root_owned_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fixture = build_chain(module, tmp_path / "root-transactions")
    install_paths(module, fixture, monkeypatch)
    runner = RootFilesRunner(module, fixture)

    chain = module._load_active_caddy_chain(runner, allow_missing=False)

    assert isinstance(chain, module.ActiveCaddyChain)
    assert chain.active_text == fixture.h1_text
    assert chain.active_sha256 == text_sha256(fixture.h1_text)
    assert chain.bootstrap_id == BOOTSTRAP_ID
    assert chain.transaction_id == T1_ID
    assert_route_payload(module, chain.installed, fixture.hardened_route)
    transaction = chain.transaction
    assert isinstance(transaction, module.PersistedCaddyTransaction)
    assert transaction.transaction_id == T1_ID
    assert transaction.operation == "switch"
    assert transaction.bootstrap_id == BOOTSTRAP_ID
    assert transaction.backup_path == fixture.t1_backup
    assert transaction.ledger_path == fixture.t1_ledger
    assert transaction.ledger_text == fixture.t1_text
    assert transaction.ledger_sha256 == text_sha256(fixture.t1_text)
    assert_route_payload(module, transaction.backup, fixture.legacy_route)
    assert_route_payload(module, transaction.installed, fixture.hardened_route)
    assert runner.mutating_commands == []


@pytest.mark.parametrize(
    "mutation",
    [
        "active-ledger-path",
        "active-ledger-byte-hash",
        "active-installed-state",
        "ledger-transaction-path",
        "ledger-backup-path",
        "backup-byte-hash",
    ],
)
def test_active_chain_rejects_broken_path_hash_or_installed_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = load_deploy_tool()
    fixture = build_chain(module, tmp_path / "root-transactions")
    install_paths(module, fixture, monkeypatch)

    active = copy.deepcopy(fixture.h1_payload)
    ledger = copy.deepcopy(fixture.t1_payload)
    if mutation == "active-ledger-path":
        wrong_ledger = fixture.root / f"commerce-ops-desk.{OTHER_ID}.json"
        active["ledger_path"] = str(wrong_ledger)
        fixture.files[wrong_ledger] = fixture.t1_text
    elif mutation == "active-ledger-byte-hash":
        active["ledger_sha256"] = "f" * 64
    elif mutation == "active-installed-state":
        active["installed"] = fixture.legacy_route
    elif mutation == "ledger-transaction-path":
        ledger["transaction_id"] = OTHER_ID
    elif mutation == "ledger-backup-path":
        wrong_backup = fixture.root / f"commerce-ops-desk.{OTHER_ID}.conf"
        assert isinstance(ledger["backup"], dict)
        ledger["backup"]["path"] = str(wrong_backup)
        fixture.files[wrong_backup] = fixture.legacy_fragment
    else:
        assert mutation == "backup-byte-hash"
        fixture.files[fixture.t1_backup] += "# tampered\n"

    if mutation in {"ledger-transaction-path", "ledger-backup-path"}:
        ledger_text = json_record(ledger)
        fixture.files[fixture.t1_ledger] = ledger_text
        active["ledger_sha256"] = text_sha256(ledger_text)
    fixture.files[fixture.active_path] = json_record(active)
    runner = RootFilesRunner(module, fixture)

    with pytest.raises(module.DeploymentError):
        module._load_active_caddy_chain(runner, allow_missing=False)

    assert runner.mutating_commands == []


def test_active_chain_allows_an_explicitly_missing_bootstrap_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fixture = build_chain(module, tmp_path / "root-transactions")
    fixture.files.pop(fixture.active_path)
    install_paths(module, fixture, monkeypatch)
    runner = RootFilesRunner(module, fixture)

    assert module._load_active_caddy_chain(runner, allow_missing=True) is None
    with pytest.raises(module.DeploymentError):
        module._load_active_caddy_chain(runner, allow_missing=False)
    assert runner.mutating_commands == []


@pytest.mark.parametrize("record_kind", ["active", "ledger"])
def test_active_chain_rejects_noncanonical_json_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_kind: str,
) -> None:
    module = load_deploy_tool()
    fixture = build_chain(module, tmp_path / "root-transactions")
    install_paths(module, fixture, monkeypatch)
    if record_kind == "active":
        fixture.files[fixture.active_path] = (
            json.dumps(fixture.h1_payload, sort_keys=True, indent=2) + "\n"
        )
    else:
        noncanonical_ledger = (
            json.dumps(fixture.t1_payload, sort_keys=True, indent=2) + "\n"
        )
        fixture.files[fixture.t1_ledger] = noncanonical_ledger
        active = copy.deepcopy(fixture.h1_payload)
        active["ledger_sha256"] = text_sha256(noncanonical_ledger)
        fixture.files[fixture.active_path] = json_record(active)
    runner = RootFilesRunner(module, fixture)

    with pytest.raises(module.DeploymentError, match="canonical"):
        module._load_active_caddy_chain(runner, allow_missing=False)

    assert runner.mutating_commands == []


def test_only_the_current_head_backup_is_authorized_and_t1_cannot_be_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fixture = build_chain(module, tmp_path / "root-transactions")
    install_paths(module, fixture, monkeypatch)
    runner = RootFilesRunner(module, fixture)

    fixture.publish_h1()
    first = module._load_validated_backup(runner, fixture.t1_backup)
    assert first.path == fixture.t1_backup
    assert first.fragment == fixture.legacy_fragment
    assert first.upstream_port == 18_087
    assert first.profile == module.CADDY_PROFILE_LEGACY_V020

    fixture.publish_h2()
    second = module._load_validated_backup(runner, fixture.t2_backup)
    assert second.path == fixture.t2_backup
    assert second.fragment == fixture.hardened_fragment
    assert second.upstream_port == 18_088
    assert second.profile == module.CADDY_PROFILE_HARDENED

    runner.reset_commands()
    with pytest.raises(module.DeploymentError):
        module._load_validated_backup(runner, fixture.t1_backup)

    assert fixture.t1_backup not in runner.cat_paths()
    assert runner.mutating_commands == []
