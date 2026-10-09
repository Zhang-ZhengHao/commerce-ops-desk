"""Adversarial contracts for the workstation Caddy transaction boundary."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
CADDY_TEMPLATE = (
    PRODUCT_ROOT / "deploy" / "workstation" / "commerce-ops-desk.Caddyfile.template"
)
LEGACY_CADDY_TEMPLATE = (
    PRODUCT_ROOT
    / "deploy"
    / "workstation"
    / "commerce-ops-desk.v0.2.0.Caddyfile.template"
)
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def sample_upstream(module: ModuleType) -> object:
    return module._validate_upstream_identity_payload(
        {
            "schema": 1,
            "docker_daemon_id": "local-daemon-id",
            "container_id": "1" * 64,
            "container_name": "app-commerce-ops-desk-candidate-0123456789ab",
            "image_id": "sha256:" + "2" * 64,
            "image_reference": f"commerce-ops-desk:{VALID_SHA}",
            "source_sha": VALID_SHA,
            "host_port": 18088,
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
    )


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_caddy_transaction", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AdaptRunner:
    """Return deterministic adapted JSON without requiring Caddy in unit tests."""

    def __init__(self, documents: dict[str, dict[str, object]]) -> None:
        self.documents = documents
        self.inputs: list[str] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        assert command[:2] == ["/usr/bin/caddy", "adapt"]
        assert input_text is not None
        self.inputs.append(input_text)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(self.documents[input_text]),
            "",
        )


def test_frozen_legacy_template_is_the_observed_v020_site() -> None:
    assert LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8") == (
        "http://commerce-ops-desk.srrsh.aig.rest {\n"
        "\treverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n"
        "}\n"
    )


def test_fragment_validation_compares_adapted_single_site_structure() -> None:
    module = load_deploy_tool()
    revision = "a" * 64
    canonical = module.render_caddy_template(
        CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18088,
        route_revision=revision,
    )
    legacy = LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{UPSTREAM_PORT}}", "18087"
    )
    canonical_at_legacy_port = module.render_caddy_template(
        CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18087,
        route_revision=revision,
    )
    legacy_at_canonical_port = LEGACY_CADDY_TEMPLATE.read_text(
        encoding="utf-8"
    ).replace("{{UPSTREAM_PORT}}", "18088")
    equivalent = canonical.replace("\t", "    ")
    injected = canonical + "\nhttp://attacker.example { respond 200 }\n"
    canonical_json = {"apps": {"http": {"servers": {"srv0": {"routes": ["managed"]}}}}}
    legacy_json = {"apps": {"http": {"servers": {"srv0": {"routes": ["legacy"]}}}}}
    runner = AdaptRunner(
        {
            canonical: canonical_json,
            equivalent: canonical_json,
            legacy: legacy_json,
            canonical_at_legacy_port: {"managed": 18087},
            legacy_at_canonical_port: {"legacy": 18088},
            injected: {
                "apps": {
                    "http": {
                        "servers": {
                            "srv0": {"routes": ["managed"]},
                            "srv1": {"routes": ["attacker"]},
                        }
                    }
                }
            },
        }
    )

    assert module._validate_managed_fragment(
        runner,
        equivalent,
        expected_upstream_port=18088,
        expected_route_revision=revision,
        allowed_profiles=frozenset({module.CADDY_PROFILE_HARDENED}),
    ) == module.ValidatedCaddyFragment(
        profile=module.CADDY_PROFILE_HARDENED,
        upstream_port=18088,
        route_revision=revision,
    )
    assert module._validate_managed_fragment(
        runner,
        legacy,
        expected_upstream_port=18087,
        allowed_profiles=frozenset({module.CADDY_PROFILE_LEGACY_V020}),
    ) == module.ValidatedCaddyFragment(
        profile=module.CADDY_PROFILE_LEGACY_V020,
        upstream_port=18087,
        route_revision=None,
    )
    with pytest.raises(module.DeploymentError, match="route revision"):
        module._validate_managed_fragment(
            runner,
            canonical,
            expected_upstream_port=18088,
            expected_route_revision="b" * 64,
            allowed_profiles=frozenset({module.CADDY_PROFILE_HARDENED}),
        )
    with pytest.raises(module.DeploymentError, match="profile is not permitted"):
        module._validate_managed_fragment(
            runner,
            legacy,
            expected_upstream_port=18087,
            allowed_profiles=frozenset({module.CADDY_PROFILE_HARDENED}),
        )
    with pytest.raises(module.DeploymentError, match="managed single-site structure"):
        module._validate_managed_fragment(
            runner,
            injected,
            expected_upstream_port=18088,
            allowed_profiles=frozenset(
                {
                    module.CADDY_PROFILE_HARDENED,
                    module.CADDY_PROFILE_LEGACY_V020,
                }
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.replace(
            "http://commerce-ops-desk.srrsh.aig.rest",
            "http://commerce-ops-desk.srrsh.aig.rest, http://attacker.example",
            1,
        ),
        lambda value: value.replace(
            "\trequest_body {", "\trespond /injected 200\n\n\trequest_body {", 1
        ),
        lambda value: value.replace("127.0.0.1:18088", "127.0.0.1:18089", 1),
    ],
)
def test_fragment_validation_rejects_address_directive_or_upstream_injection(
    mutation: Any,
) -> None:
    module = load_deploy_tool()
    canonical = CADDY_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{UPSTREAM_PORT}}", "18088"
    )
    malicious = mutation(canonical)
    legacy = LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{UPSTREAM_PORT}}", "18088"
    )
    runner = AdaptRunner(
        {
            canonical: {"managed": 18088},
            legacy: {"legacy": 18088},
            malicious: {"unexpected": malicious},
        }
    )

    with pytest.raises(module.DeploymentError):
        module._validate_managed_fragment(
            runner,
            malicious,
            expected_upstream_port=18088,
            allowed_profiles=module.CADDY_MANAGED_PROFILES,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.replace(
            "http://commerce-ops-desk.srrsh.aig.rest",
            "http://commerce-ops-desk.srrsh.aig.rest, http://attacker.example",
            1,
        ),
        lambda value: value.replace(
            "\treverse_proxy", "\trespond /injected 200\n\treverse_proxy", 1
        ),
        lambda value: value + "\nhttp://attacker.example { respond 200 }\n",
    ],
)
def test_legacy_profile_rejects_extra_addresses_directives_or_sites(
    mutation: Any,
) -> None:
    module = load_deploy_tool()
    canonical = CADDY_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{UPSTREAM_PORT}}", "18087"
    )
    legacy = LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{UPSTREAM_PORT}}", "18087"
    )
    malicious = mutation(legacy)
    runner = AdaptRunner(
        {
            canonical: {"managed": 18087},
            legacy: {"legacy": 18087},
            malicious: {"unexpected": malicious},
        }
    )

    with pytest.raises(module.DeploymentError, match="managed single-site structure"):
        module._validate_managed_fragment(
            runner,
            malicious,
            expected_upstream_port=18087,
            allowed_profiles=module.CADDY_MANAGED_PROFILES,
        )


def test_switch_holds_one_lock_across_head_loading_and_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    events: list[str] = []
    lock_held = False
    state = {
        "source_sha": VALID_SHA,
        "candidate_port": 18088,
    }
    verified_template = (
        "http://commerce-ops-desk.srrsh.aig.rest {\n"
        "\treverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n"
        '\theader X-CommerceOps-Route-Revision "{{ROUTE_REVISION}}"\n'
        "\t# verified in-memory template\n"
        "}\n"
    )
    verified_assets = module.VerifiedDeploymentAssets(
        identity={
            "schema": 1,
            "sha256": {path: "a" * 64 for path in module.DEPLOYMENT_ASSET_PATHS},
        },
        compose_yaml="verified compose\n",
        caddy_templates={
            module.CADDY_PROFILE_HARDENED: verified_template,
            module.CADDY_PROFILE_LEGACY_V020: LEGACY_CADDY_TEMPLATE.read_text(
                encoding="utf-8"
            ),
        },
    )
    upstream = sample_upstream(module)
    verified_candidate = module.VerifiedCandidate(
        deployment_assets=verified_assets,
        upstream=upstream,
    )
    revision = module._route_revision(
        module.CADDY_PROFILE_HARDENED,
        upstream,
        verified_assets.identity,
    )
    current_route = object()
    parent = SimpleNamespace(installed=current_route)

    @contextmanager
    def transaction_lock(_runner: object) -> Iterator[None]:
        nonlocal lock_held
        assert lock_held is False
        lock_held = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            lock_held = False

    def in_lock(event: str) -> None:
        assert lock_held is True
        events.append(event)

    monkeypatch.setattr(
        module, "_caddy_transaction_lock", transaction_lock, raising=False
    )
    monkeypatch.setattr(
        module,
        "_load_candidate_state",
        lambda _path: in_lock("load-state") or state,
    )
    monkeypatch.setattr(
        module,
        "_assert_state_candidate_ready",
        lambda _runner, _state: in_lock("candidate-ready") or verified_candidate,
    )
    monkeypatch.setattr(
        module,
        "_load_active_caddy_chain",
        lambda _runner, *, allow_missing: (
            in_lock("load-head")
            or (parent if allow_missing is True else pytest.fail("switch head policy"))
        ),
        raising=False,
    )

    def install_candidate(
        _runner: object,
        fragment: str,
        **kwargs: object,
    ) -> Path:
        assert fragment == module.render_caddy_template(
            verified_template,
            18088,
            route_revision=revision,
        )
        assert kwargs["operation"] == "switch"
        assert kwargs["caddy_templates"] is verified_assets.caddy_templates
        assert kwargs["current_route"] is current_route
        assert kwargs["parent"] is parent
        installed_route = kwargs["installed_route"]
        assert isinstance(installed_route, module.RouteState)
        assert (
            installed_route.fragment_sha256
            == hashlib.sha256(fragment.encode("utf-8")).hexdigest()
        )
        assert installed_route.profile == module.CADDY_PROFILE_HARDENED
        assert installed_route.route_revision == revision
        assert installed_route.upstream is upstream
        assert installed_route.deployment_assets == verified_assets.identity
        assert "expected_upstream_port" not in kwargs
        in_lock("install")
        return Path("switch-backup")

    monkeypatch.setattr(module, "_install_caddy_fragment", install_candidate)

    module.switch_candidate(argparse.Namespace(state=str(tmp_path / "state")), object())

    assert events[0] == "lock-enter"
    assert events[-1] == "lock-exit"
    assert sorted(events[1:-1]) == sorted(
        ["load-state", "candidate-ready", "load-head", "install"]
    )
    assert lock_held is False


def test_rollback_holds_one_lock_across_current_head_authorization_and_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    events: list[str] = []
    lock_held = False
    current_route = object()
    rollback_route = object()
    parent = SimpleNamespace(
        installed=current_route,
        transaction=SimpleNamespace(backup=rollback_route),
    )

    trusted_backup = module.TrustedCaddyBackup(
        path=tmp_path / "trusted.conf",
        fragment="trusted",
        upstream_port=18087,
        profile=module.CADDY_PROFILE_LEGACY_V020,
        parent=parent,
    )

    @contextmanager
    def transaction_lock(_runner: object) -> Iterator[None]:
        nonlocal lock_held
        assert lock_held is False
        lock_held = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            lock_held = False

    def in_lock(event: str) -> None:
        assert lock_held is True
        events.append(event)

    monkeypatch.setattr(
        module, "_caddy_transaction_lock", transaction_lock, raising=False
    )
    monkeypatch.setattr(
        module,
        "_load_validated_backup",
        lambda _runner, _path: in_lock("authorize-backup") or trusted_backup,
    )

    def install_rollback(
        _runner: object,
        fragment: str,
        **kwargs: object,
    ) -> Path:
        assert fragment == trusted_backup.fragment
        assert kwargs["operation"] == "rollback"
        assert kwargs["current_route"] is current_route
        assert kwargs["installed_route"] is rollback_route
        assert kwargs["parent"] is parent
        assert "expected_upstream_port" not in kwargs
        in_lock("install")
        return Path("rollback-safety")

    monkeypatch.setattr(module, "_install_caddy_fragment", install_rollback)

    module.rollback_site(argparse.Namespace(backup=str(trusted_backup.path)), object())

    assert events[0] == "lock-enter"
    assert events[-1] == "lock-exit"
    assert events[1:-1] == ["authorize-backup", "install"]
    assert lock_held is False


class RootLedgerRunner:
    def __init__(
        self,
        *,
        directory: Path,
        files: dict[Path, str],
    ) -> None:
        self.directory = directory
        self.files = files
        self.read_paths: list[Path] = []

    def run(
        self, arguments: Sequence[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        if command[1] == "test":
            predicate = command[2]
            path = Path(command[-1])
            exists = path == self.directory or path in self.files
            if predicate == "-L":
                return subprocess.CompletedProcess(command, 1, "", "")
            if predicate in {"-e", "-f"}:
                return subprocess.CompletedProcess(
                    command,
                    0 if exists else 1,
                    "",
                    "",
                )
            raise AssertionError(f"unexpected test predicate: {predicate}")
        if command[1:3] == ["stat", "--format=%f|%u|%g|%h"]:
            path = Path(command[-1])
            if path == self.directory:
                output = "41c0|0|0|2\n"
            elif path in self.files:
                output = "8180|0|0|1\n"
            else:  # pragma: no cover - makes an unexpected access obvious.
                raise AssertionError(f"unexpected stat path: {path}")
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[1] == "cat":
            path = Path(command[-1])
            self.read_paths.append(path)
            return subprocess.CompletedProcess(command, 0, self.files[path], "")
        raise AssertionError(f"unexpected command: {command}")


def _schema2_rollback_fixture(
    module: ModuleType,
    transaction_directory: Path,
) -> tuple[Path, Path, Path, str, str, str]:
    transaction_id = "20261009T120000Z-0123456789abcdef0123456789abcdef"
    backup = transaction_directory / f"commerce-ops-desk.{transaction_id}.conf"
    ledger = backup.with_suffix(".json")
    active = transaction_directory / "active.json"
    fragment = module.render_caddy_template(
        LEGACY_CADDY_TEMPLATE.read_text(encoding="utf-8"),
        18_087,
    )
    legacy_record = {
        "schema": 2,
        "transaction_id": transaction_id,
        "operation": "switch",
        "site_path": str(module.CADDY_SITE),
        "site_host": module.CADDY_HOST,
        "backup_path": str(backup),
        "backup_sha256": hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
        "backup_upstream_port": 18_087,
        "backup_profile": module.CADDY_PROFILE_LEGACY_V020,
        "installed_sha256": "f" * 64,
        "installed_upstream_port": 18_088,
        "installed_profile": module.CADDY_PROFILE_HARDENED,
    }
    ledger_text = (
        json.dumps(
            legacy_record,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    active_record = {
        "schema": module.ACTIVE_STATE_SCHEMA,
        "bootstrap_id": "b" * 64,
        "transaction_id": transaction_id,
        "ledger_path": str(ledger),
        "ledger_sha256": hashlib.sha256(ledger_text.encode("utf-8")).hexdigest(),
        "site": {
            "path": str(module.CADDY_SITE),
            "host": module.CADDY_HOST,
        },
        "installed": {
            "fragment_sha256": hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
            "profile": module.CADDY_PROFILE_LEGACY_V020,
            "route_revision": None,
            "upstream": module._upstream_identity_payload(sample_upstream(module)),
            "deployment_assets": None,
        },
    }
    active_text = (
        json.dumps(
            active_record,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return active, backup, ledger, active_text, ledger_text, fragment


def test_schema2_rollback_ledger_is_rejected_before_the_backup_is_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory = tmp_path / "root-caddy-transactions"
    active, backup, ledger, active_text, ledger_text, fragment = (
        _schema2_rollback_fixture(module, transaction_directory)
    )
    monkeypatch.setattr(module, "CADDY_TRANSACTION_DIRECTORY", transaction_directory)
    monkeypatch.setattr(module, "CADDY_ACTIVE_STATE", active, raising=False)
    runner = RootLedgerRunner(
        directory=transaction_directory,
        files={
            active: active_text,
            ledger: ledger_text,
            backup: fragment,
        },
    )

    def reject_fragment_validation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("schema 2 rollback reached fragment validation")

    monkeypatch.setattr(
        module,
        "_validate_managed_fragment",
        reject_fragment_validation,
    )

    with pytest.raises(module.DeploymentError, match="transaction ledger schema"):
        module._load_validated_backup(runner, backup)

    assert runner.read_paths == [active, ledger]
    assert backup not in runner.read_paths
