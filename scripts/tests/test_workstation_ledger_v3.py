"""Schema and identity contracts for the workstation route ledger."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
TRANSACTION_ID = "20261009T120000Z-0123456789abcdef0123456789abcdef"
PARENT_TRANSACTION_ID = "20261009T110000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BOOTSTRAP_ID = "b" * 64
UPSTREAM_HASH_DOMAIN = b"commerce-ops-desk-upstream-v1\0"
ROUTE_REVISION_HASH_DOMAIN = b"commerce-ops-desk-route-v1\0"
DEPLOYMENT_ASSET_PATHS = (
    "deploy/workstation/compose.yaml",
    "deploy/workstation/commerce-ops-desk.Caddyfile.template",
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template",
)


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_ledger_v3", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_sha256(domain: bytes, payload: object) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + canonical).hexdigest()


def deployment_assets_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "sha256": {
            DEPLOYMENT_ASSET_PATHS[0]: "6" * 64,
            DEPLOYMENT_ASSET_PATHS[1]: "7" * 64,
            DEPLOYMENT_ASSET_PATHS[2]: "8" * 64,
        },
    }


def upstream_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "docker_daemon_id": "local-daemon-id",
        "container_id": "1" * 64,
        "container_name": "app-commerce-ops-desk-candidate-0123456789ab",
        "image_id": "sha256:" + "2" * 64,
        "image_reference": f"commerce-ops-desk:{SOURCE_SHA}",
        "source_sha": SOURCE_SHA,
        "host_port": 18_088,
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


def expected_upstream_fingerprint(payload: dict[str, object]) -> str:
    return canonical_sha256(UPSTREAM_HASH_DOMAIN, payload)


def expected_route_revision(
    *,
    profile: str,
    upstream: dict[str, object],
    deployment_assets: dict[str, object],
) -> str:
    return canonical_sha256(
        ROUTE_REVISION_HASH_DOMAIN,
        {
            "profile": profile,
            "upstream": upstream,
            "deployment_assets": deployment_assets,
        },
    )


def route_state_payload(
    *,
    profile: str = "hardened-v1",
    legacy_without_revision: bool = False,
) -> dict[str, object]:
    upstream = upstream_payload()
    assets = None if legacy_without_revision else deployment_assets_payload()
    revision = (
        None
        if assets is None
        else expected_route_revision(
            profile=profile,
            upstream=upstream,
            deployment_assets=assets,
        )
    )
    return {
        "fragment_sha256": "9" * 64,
        "profile": profile,
        "route_revision": revision,
        "upstream": upstream,
        "deployment_assets": assets,
    }


def active_state_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "bootstrap_id": BOOTSTRAP_ID,
        "transaction_id": TRANSACTION_ID,
        "ledger_path": (
            "/var/lib/commerce-ops-desk/caddy-transactions/"
            f"commerce-ops-desk.{TRANSACTION_ID}.json"
        ),
        "ledger_sha256": "a" * 64,
        "site": {
            "path": "/etc/caddy/sites/commerce-ops-desk.conf",
            "host": "commerce-ops-desk.srrsh.aig.rest",
        },
        "installed": route_state_payload(),
    }


def transaction_ledger_payload(
    *,
    parent_transaction_id: str | None = PARENT_TRANSACTION_ID,
    parent_active_sha256: str | None = "c" * 64,
) -> dict[str, object]:
    backup = {
        "path": (
            "/var/lib/commerce-ops-desk/caddy-transactions/"
            f"commerce-ops-desk.{TRANSACTION_ID}.conf"
        ),
        **route_state_payload(
            profile="legacy-v0.2.0",
            legacy_without_revision=True,
        ),
    }
    return {
        "schema": 3,
        "transaction_id": TRANSACTION_ID,
        "operation": "switch",
        "bootstrap_id": BOOTSTRAP_ID,
        "parent": {
            "transaction_id": parent_transaction_id,
            "active_sha256": parent_active_sha256,
        },
        "site": {
            "path": "/etc/caddy/sites/commerce-ops-desk.conf",
            "host": "commerce-ops-desk.srrsh.aig.rest",
        },
        "backup": backup,
        "installed": route_state_payload(),
    }


def test_upstream_identity_accepts_only_the_exact_schema() -> None:
    module = load_deploy_tool()
    payload = upstream_payload()

    validated = module._validate_upstream_identity_payload(payload)

    assert validated == module.UpstreamIdentity(**payload)


@pytest.mark.parametrize("mutation", ["missing", "extra", "non-mapping"])
def test_upstream_identity_rejects_non_exact_field_sets(mutation: str) -> None:
    module = load_deploy_tool()
    payload: object = upstream_payload()
    if mutation == "missing":
        assert isinstance(payload, dict)
        payload.pop("runtime_sha256")
    elif mutation == "extra":
        assert isinstance(payload, dict)
        payload["project_name"] = "legacy-field"
    else:
        payload = list(upstream_payload().items())

    with pytest.raises(module.DeploymentError, match="upstream identity"):
        module._validate_upstream_identity_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("schema", True),
        ("schema", 2),
        ("docker_daemon_id", ""),
        ("container_id", "1" * 63),
        ("container_name", 7),
        ("image_id", "2" * 64),
        ("image_reference", 7),
        ("source_sha", SOURCE_SHA.upper()),
        ("host_port", True),
        ("host_port", "18088"),
        ("host_port", 1_023),
        ("data_path", "relative/data"),
        ("data_device", True),
        ("data_device", -1),
        ("data_inode", 0),
        ("network_name", 7),
        ("network_id", "3" * 63),
        ("network_endpoint_id", "4" * 63),
        ("runtime_sha256", "5" * 63),
    ],
)
def test_upstream_identity_rejects_wrong_schema_and_field_types(
    field: str,
    invalid: object,
) -> None:
    module = load_deploy_tool()
    payload = upstream_payload()
    payload[field] = invalid

    with pytest.raises(module.DeploymentError, match="upstream identity"):
        module._validate_upstream_identity_payload(payload)


def test_upstream_fingerprint_has_a_domain_separated_canonical_vector() -> None:
    module = load_deploy_tool()
    payload = upstream_payload()
    reordered = dict(reversed(tuple(payload.items())))
    upstream = module._validate_upstream_identity_payload(payload)
    reordered_upstream = module._validate_upstream_identity_payload(reordered)

    assert expected_upstream_fingerprint(payload) == (
        "0c4953eb6e5dc8699b69d5f54999a6c3d68bcb3146c87c1083f0f3a7e6eff449"
    )
    assert module._upstream_identity_fingerprint(upstream) == (
        expected_upstream_fingerprint(payload)
    )
    assert module._upstream_identity_fingerprint(reordered_upstream) == (
        expected_upstream_fingerprint(payload)
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("docker_daemon_id", "replacement-daemon"),
        ("container_id", "a" * 64),
        ("container_name", "replacement-container"),
        ("image_id", "sha256:" + "a" * 64),
        ("image_reference", "commerce-ops-desk:replacement"),
        ("source_sha", "a" * 40),
        ("host_port", 18_089),
        ("data_path", "/home/deploy/apps/commerce-ops-desk/replacement-data"),
        ("data_device", 64_770),
        ("data_inode", 55_451_028),
        ("network_name", "replacement-network"),
        ("network_id", "a" * 64),
        ("network_endpoint_id", "b" * 64),
        ("runtime_sha256", "c" * 64),
    ],
)
def test_upstream_fingerprint_binds_every_security_identity_field(
    field: str,
    replacement: object,
) -> None:
    module = load_deploy_tool()
    original = module._validate_upstream_identity_payload(upstream_payload())
    changed = replace(original, **{field: replacement})

    assert module._upstream_identity_fingerprint(changed) != (
        module._upstream_identity_fingerprint(original)
    )


def test_route_revision_binds_profile_upstream_and_deployment_assets() -> None:
    module = load_deploy_tool()
    upstream_raw = upstream_payload()
    assets = deployment_assets_payload()
    upstream = module._validate_upstream_identity_payload(upstream_raw)
    expected = expected_route_revision(
        profile=module.CADDY_PROFILE_HARDENED,
        upstream=upstream_raw,
        deployment_assets=assets,
    )

    assert expected == (
        "bce1233646e61d352e32afa38e10abb16f7f462d7fc5c32057433cc49f321c93"
    )
    assert (
        module._route_revision(
            module.CADDY_PROFILE_HARDENED,
            upstream,
            assets,
        )
        == expected
    )
    assert (
        module._route_revision(
            module.CADDY_PROFILE_LEGACY_V020,
            upstream,
            assets,
        )
        != expected
    )
    assert (
        module._route_revision(
            module.CADDY_PROFILE_HARDENED,
            replace(upstream, runtime_sha256="a" * 64),
            assets,
        )
        != expected
    )
    changed_assets = copy.deepcopy(assets)
    assert isinstance(changed_assets["sha256"], dict)
    changed_assets["sha256"][DEPLOYMENT_ASSET_PATHS[0]] = "f" * 64
    assert (
        module._route_revision(
            module.CADDY_PROFILE_HARDENED,
            upstream,
            changed_assets,
        )
        != expected
    )


@pytest.mark.parametrize(
    "payload",
    [
        route_state_payload(),
        route_state_payload(
            profile="legacy-v0.2.0",
            legacy_without_revision=True,
        ),
    ],
    ids=("verified", "bootstrap-legacy"),
)
def test_route_state_accepts_verified_and_explicit_unverified_legacy_states(
    payload: dict[str, object],
) -> None:
    module = load_deploy_tool()

    validated = module._validate_route_state_payload(payload)

    assert isinstance(validated, module.RouteState)
    assert validated.fragment_sha256 == payload["fragment_sha256"]
    assert validated.profile == payload["profile"]
    assert validated.route_revision == payload["route_revision"]
    assert validated.upstream == module._validate_upstream_identity_payload(
        payload["upstream"]
    )
    assert validated.deployment_assets == payload["deployment_assets"]


@pytest.mark.parametrize("mutation", ["missing", "extra", "non-mapping"])
def test_route_state_rejects_non_exact_field_sets(mutation: str) -> None:
    module = load_deploy_tool()
    payload: object = route_state_payload()
    if mutation == "missing":
        assert isinstance(payload, dict)
        payload.pop("fragment_sha256")
    elif mutation == "extra":
        assert isinstance(payload, dict)
        payload["installed_profile"] = "hardened-v1"
    else:
        payload = []

    with pytest.raises(module.DeploymentError, match="route state"):
        module._validate_route_state_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("fragment_sha256", "9" * 63),
        ("profile", "unmanaged-v1"),
        ("route_revision", True),
        ("upstream", []),
        ("deployment_assets", []),
    ],
)
def test_route_state_rejects_invalid_nested_types(
    field: str,
    invalid: object,
) -> None:
    module = load_deploy_tool()
    payload = route_state_payload()
    payload[field] = invalid

    with pytest.raises(module.DeploymentError):
        module._validate_route_state_payload(payload)


@pytest.mark.parametrize("missing_field", ["route_revision", "deployment_assets"])
def test_route_state_requires_revision_and_assets_to_be_nullable_as_a_pair(
    missing_field: str,
) -> None:
    module = load_deploy_tool()
    payload = route_state_payload()
    payload[missing_field] = None

    with pytest.raises(module.DeploymentError, match="route revision"):
        module._validate_route_state_payload(payload)


def test_route_state_rejects_a_revision_not_bound_to_its_payload() -> None:
    module = load_deploy_tool()
    payload = route_state_payload()
    payload["route_revision"] = "f" * 64

    with pytest.raises(module.DeploymentError, match="route revision"):
        module._validate_route_state_payload(payload)


def test_active_state_accepts_one_exact_linear_head() -> None:
    module = load_deploy_tool()
    payload = active_state_payload()

    assert module._validate_active_state_payload(payload) == payload


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-root",
        "extra-root",
        "old-flat-site",
        "missing-site",
        "extra-site",
        "missing-installed",
        "extra-installed",
    ],
)
def test_active_state_rejects_non_exact_root_and_nested_field_sets(
    mutation: str,
) -> None:
    module = load_deploy_tool()
    payload = active_state_payload()
    if mutation == "missing-root":
        payload.pop("ledger_sha256")
    elif mutation == "extra-root":
        payload["installed_profile"] = "hardened-v1"
    elif mutation == "old-flat-site":
        payload["site_path"] = payload["site"]["path"]
    elif mutation == "missing-site":
        payload["site"].pop("host")
    elif mutation == "extra-site":
        payload["site"]["port"] = 80
    elif mutation == "missing-installed":
        payload["installed"].pop("upstream")
    else:
        payload["installed"]["installed_sha256"] = "f" * 64

    with pytest.raises(module.DeploymentError):
        module._validate_active_state_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("schema", True),
        ("schema", 2),
        ("bootstrap_id", "b" * 63),
        ("transaction_id", "manual-head"),
        ("ledger_path", "relative-ledger.json"),
        ("ledger_sha256", "a" * 63),
        ("site", []),
        ("installed", []),
    ],
)
def test_active_state_rejects_invalid_head_types(field: str, invalid: object) -> None:
    module = load_deploy_tool()
    payload = active_state_payload()
    payload[field] = invalid

    with pytest.raises(module.DeploymentError):
        module._validate_active_state_payload(payload)


@pytest.mark.parametrize(
    ("parent_transaction_id", "parent_active_sha256"),
    [
        (None, None),
        (PARENT_TRANSACTION_ID, "c" * 64),
    ],
    ids=("bootstrap", "chained"),
)
def test_transaction_ledger_accepts_bootstrap_and_chained_parents(
    parent_transaction_id: str | None,
    parent_active_sha256: str | None,
) -> None:
    module = load_deploy_tool()
    payload = transaction_ledger_payload(
        parent_transaction_id=parent_transaction_id,
        parent_active_sha256=parent_active_sha256,
    )

    assert module._validate_transaction_ledger_payload(payload) == payload


def test_transaction_ledger_rejects_bootstrap_rollback_and_self_parent() -> None:
    module = load_deploy_tool()
    bootstrap_rollback = transaction_ledger_payload(
        parent_transaction_id=None,
        parent_active_sha256=None,
    )
    bootstrap_rollback["operation"] = "rollback"

    with pytest.raises(module.DeploymentError, match="parent|rollback"):
        module._validate_transaction_ledger_payload(bootstrap_rollback)

    self_parent = transaction_ledger_payload(
        parent_transaction_id=TRANSACTION_ID,
        parent_active_sha256="c" * 64,
    )
    with pytest.raises(module.DeploymentError, match="parent|self"):
        module._validate_transaction_ledger_payload(self_parent)


@pytest.mark.parametrize(
    ("parent_transaction_id", "parent_active_sha256"),
    [
        (None, "c" * 64),
        (PARENT_TRANSACTION_ID, None),
        ("manual-parent", "c" * 64),
        (PARENT_TRANSACTION_ID, "c" * 63),
    ],
)
def test_transaction_ledger_rejects_partial_or_invalid_parent_heads(
    parent_transaction_id: str | None,
    parent_active_sha256: str | None,
) -> None:
    module = load_deploy_tool()
    payload = transaction_ledger_payload(
        parent_transaction_id=parent_transaction_id,
        parent_active_sha256=parent_active_sha256,
    )

    with pytest.raises(module.DeploymentError, match="parent"):
        module._validate_transaction_ledger_payload(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-root",
        "extra-root",
        "old-flat-ledger",
        "missing-parent",
        "extra-parent",
        "missing-site",
        "extra-site",
        "missing-backup",
        "extra-backup",
        "missing-installed",
        "extra-installed",
    ],
)
def test_transaction_ledger_rejects_non_exact_nested_field_sets(
    mutation: str,
) -> None:
    module = load_deploy_tool()
    payload = transaction_ledger_payload()
    if mutation == "missing-root":
        payload.pop("bootstrap_id")
    elif mutation == "extra-root":
        payload["created_at"] = "2026-10-09T12:00:00Z"
    elif mutation == "old-flat-ledger":
        payload["backup_path"] = payload["backup"]["path"]
        payload["backup_profile"] = payload["backup"]["profile"]
        payload["installed_profile"] = payload["installed"]["profile"]
    elif mutation == "missing-parent":
        payload["parent"].pop("active_sha256")
    elif mutation == "extra-parent":
        payload["parent"]["ledger_sha256"] = "d" * 64
    elif mutation == "missing-site":
        payload["site"].pop("host")
    elif mutation == "extra-site":
        payload["site"]["port"] = 80
    elif mutation == "missing-backup":
        payload["backup"].pop("fragment_sha256")
    elif mutation == "extra-backup":
        payload["backup"]["backup_sha256"] = "f" * 64
    elif mutation == "missing-installed":
        payload["installed"].pop("profile")
    else:
        payload["installed"]["installed_sha256"] = "f" * 64

    with pytest.raises(module.DeploymentError):
        module._validate_transaction_ledger_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("schema", True),
        ("schema", 2),
        ("transaction_id", "manual-transaction"),
        ("operation", "restore"),
        ("bootstrap_id", "b" * 63),
        ("parent", []),
        ("site", []),
        ("backup", []),
        ("installed", []),
    ],
)
def test_transaction_ledger_rejects_invalid_schema_and_types(
    field: str,
    invalid: object,
) -> None:
    module = load_deploy_tool()
    payload = transaction_ledger_payload()
    payload[field] = invalid

    with pytest.raises(module.DeploymentError):
        module._validate_transaction_ledger_payload(payload)


def candidate_state_payload(module: ModuleType, tmp_path: Path) -> dict[str, object]:
    identity = module.candidate_identity(SOURCE_SHA)
    return {
        "schema": 2,
        "source_sha": SOURCE_SHA,
        "project_name": identity.project_name,
        "container_name": identity.container_name,
        "container_id": "1" * 64,
        "image": identity.image,
        "image_id": "sha256:" + "2" * 64,
        "docker_daemon_id": "local-daemon-id",
        "candidate_port": 18_088,
        "candidate_data_dir": str(tmp_path / "candidate"),
        "candidate_data_device": 64_769,
        "candidate_data_inode": 55_451_027,
        "live_data_dir": str(tmp_path / "live"),
        "live_data_device": 64_769,
        "live_data_inode": 55_451_026,
        "network_name": f"{identity.project_name}_default",
        "network_id": "3" * 64,
        "network_endpoint_id": "4" * 64,
        "runtime_sha256": "5" * 64,
        "trusted_proxy_cidrs": ["192.0.2.1/32"],
        "deployment_assets": deployment_assets_payload(),
    }


def test_candidate_revalidation_returns_all_verified_upstream_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state = candidate_state_payload(module, tmp_path)
    identity = module.candidate_identity(SOURCE_SHA)
    assets = module.VerifiedDeploymentAssets(
        identity=MappingProxyType(deployment_assets_payload()),
        compose_yaml="verified-compose",
        caddy_templates=MappingProxyType({}),
    )
    monkeypatch.setattr(
        module, "_load_verified_deployment_assets", lambda _value: assets
    )
    monkeypatch.setattr(
        module,
        "validate_data_directories",
        lambda **_kwargs: (tmp_path / "live", tmp_path / "candidate"),
    )
    monkeypatch.setattr(module, "validate_deployment_layout", lambda **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_assert_data_directory_identity",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(module, "_assert_local_docker_daemon", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_assert_exact_project_container",
        lambda *_args: "1" * 64,
    )
    monkeypatch.setattr(
        module,
        "_validate_candidate_network_contract",
        lambda *_args, **_kwargs: module.CandidateNetworkContract(
            network_id="3" * 64,
            subnet="192.0.2.0/24",
            gateway="192.0.2.1",
            endpoint_id="4" * 64,
            ipv4_address="192.0.2.2/24",
        ),
    )
    monkeypatch.setattr(
        module, "_assert_image_revision", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(module, "_immutable_image_config", lambda *_args: {})
    monkeypatch.setattr(module, "_wait_for_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_assert_candidate_runtime",
        lambda *_args, **_kwargs: "5" * 64,
    )

    class NetworkRunner:
        def run(
            self,
            arguments: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            assert arguments == [
                module.DOCKER_BINARY,
                "network",
                "inspect",
                f"{identity.project_name}_default",
            ]
            return subprocess.CompletedProcess(arguments, 0, "[]", "")

    verified = module._assert_state_candidate_ready(NetworkRunner(), state)

    assert isinstance(verified, module.VerifiedCandidate)
    assert verified.deployment_assets is assets
    assert verified.upstream == module.UpstreamIdentity(
        schema=1,
        docker_daemon_id="local-daemon-id",
        container_id="1" * 64,
        container_name=identity.container_name,
        image_id="sha256:" + "2" * 64,
        image_reference=identity.image,
        source_sha=SOURCE_SHA,
        host_port=18_088,
        data_path=str(tmp_path / "candidate"),
        data_device=64_769,
        data_inode=55_451_027,
        network_name=f"{identity.project_name}_default",
        network_id="3" * 64,
        network_endpoint_id="4" * 64,
        runtime_sha256="5" * 64,
    )
