"""Contracts for candidate build provenance and reserved image labels."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_MODULE = PRODUCT_ROOT / "deploy" / "workstation" / "verified_provenance.py"
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"

TARGET_SHA = "0123456789abcdef0123456789abcdef01234567"
DONOR_SHA = "fedcba9876543210fedcba9876543210fedcba98"
OTHER_DONOR_SHA = "abcdef0123456789abcdef0123456789abcdef01"
GRANDDONOR_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TARGET_IMAGE_ID = f"sha256:{'1' * 64}"
DONOR_IMAGE_ID = f"sha256:{'2' * 64}"
OTHER_DONOR_IMAGE_ID = f"sha256:{'3' * 64}"
GRANDDONOR_IMAGE_ID = f"sha256:{'4' * 64}"
SCHEMA2_MANIFEST_SHA256 = (
    "b4d76652d8c9b149768f72b75ed2219458a3b966ed41d2f5795acaeddd37a4d3"
)
SCHEMA3_MANIFEST_SHA256 = (
    "b608ed33fda2a997299fe299b159f87f9d3b9d659a1b80725d68261f7b9cce58"
)
REUSE_MANIFEST_SHA256 = (
    "181f9cf90b1d246d1584336276f6ea691b6655adb0ea344e118ee1effa62151d"
)
COMBINED_RUNTIME_DIGEST = (
    "b3b1f9868a0e8ca6fb608f7752d057a280e381d45925e831b2c00f70d0b5713a"
)
RUNTIME_COMPONENT_DIGESTS = {
    "backend": "a" * 64,
    "dockerignore": "b" * 64,
    "frontend": "c" * 64,
    "scripts": "d" * 64,
}
DEPLOYMENT_ASSET_DIGESTS = {
    "deploy/workstation/compose.yaml": "a" * 64,
    "deploy/workstation/commerce-ops-desk.Caddyfile.template": "b" * 64,
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template": "c" * 64,
}

CANDIDATE_PROVENANCE_DOMAIN = b"commerce-ops-desk:candidate-build-provenance:v1\0"
CANDIDATE_STATE_FIELDS = frozenset(
    {
        "schema",
        "source_sha",
        "project_name",
        "container_name",
        "container_id",
        "image",
        "image_id",
        "docker_daemon_id",
        "candidate_port",
        "candidate_data_dir",
        "candidate_data_device",
        "candidate_data_inode",
        "live_data_dir",
        "live_data_device",
        "live_data_inode",
        "network_name",
        "network_id",
        "network_endpoint_id",
        "runtime_sha256",
        "trusted_proxy_cidrs",
        "deployment_assets",
        "build_provenance",
        "build_provenance_sha256",
    }
)
RESERVED_LABEL_PREFIX = "io.github.zhang-zhenghao.commerce-ops."
BUILD_KIND_LABEL = f"{RESERVED_LABEL_PREFIX}build-kind"
REUSED_FROM_SOURCE_LABEL = f"{RESERVED_LABEL_PREFIX}reused-from-source"
REUSED_FROM_IMAGE_ID_LABEL = f"{RESERVED_LABEL_PREFIX}reused-from-image-id"
RUNTIME_INPUTS_LABEL = f"{RESERVED_LABEL_PREFIX}runtime-inputs-sha256"

SCHEMA2_SOURCE_CANONICAL = (
    b'{"kind":"source-build","schema":1,"source_manifest_schema":2,'
    b'"source_manifest_sha256":"b4d76652d8c9b149768f72b75ed2219458a3b966ed41d2f'
    b'5795acaeddd37a4d3"}'
)
SCHEMA3_SOURCE_CANONICAL = (
    b'{"kind":"source-build","schema":1,"source_manifest_schema":3,'
    b'"source_manifest_sha256":"b608ed33fda2a997299fe299b159f87f9d3b9d659a1b807'
    b'25d68261f7b9cce58"}'
)
REUSE_CANONICAL = (
    b'{"kind":"verified-runtime-reuse","reused_from":{"image_id":"sha256:'
    b'2222222222222222222222222222222222222222222222222222222222222222",'
    b'"image_reference":"commerce-ops-desk:fedcba9876543210fedcba9876543210'
    b'fedcba98","manifest_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
    b'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","source_sha":"fedcba9876543210fedcba9876543210'
    b'fedcba98"},"runtime_equivalence":{"components":{"backend":{"donor_sha256":'
    b'"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"target_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    b'aaaaaaaa"},"dockerignore":{"donor_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    b'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","target_sha256":"bbbbbbbbbbbbbbbbbbbb'
    b'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"frontend":{"donor_sha256":'
    b'"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
    b'"target_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
    b'cccc"},"scripts":{"donor_sha256":"dddddddddddddddddddddddddddddddd'
    b'dddddddddddddddddddddddddddddddd","target_sha256":"dddddddddddddddddddddddddddddddd'
    b'dddddddddddddddddddddddddddddddd"}},"dockerfile_transition":{"from_sha256":'
    b'"3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a",'
    b'"policy":"pip-timeout-300-v1","to_sha256":"35258f0647df1dd08e0da3a17749'
    b'eb1b77a4b6086475c18e5c11a954c3c6a69d"},"donor_copy_inputs_sha256":'
    b'"b3b1f9868a0e8ca6fb608f7752d057a280e381d45925e831b2c00f70d0b5713a",'
    b'"policy":"runtime-copy-inputs-v1+pip-timeout-300-v1","schema":1,'
    b'"target_copy_inputs_sha256":"b3b1f9868a0e8ca6fb608f7752d057a280e381d459'
    b'25e831b2c00f70d0b5713a"},"schema":1,"source_manifest_sha256":'
    b'"181f9cf90b1d246d1584336276f6ea691b6655adb0ea344e118ee1effa62151d"}'
)

GOLDEN_CASES = (
    (
        "schema2-source",
        SCHEMA2_SOURCE_CANONICAL,
        "c2c602402b309fd112dfe72ddd9535568b55ef508541d2fb2697742b6b2313e3",
    ),
    (
        "schema3-source",
        SCHEMA3_SOURCE_CANONICAL,
        "9b360fa7e2312fe7d4306c6e2a6f6fdf99ed0989ff46242cc1b9f2e645052fd9",
    ),
    (
        "schema3-reuse",
        REUSE_CANONICAL,
        "04c0062adc42de7aa093d49791cf906bc76b02d778d29598236dd79c78947143",
    ),
)

REQUIRED_PUBLIC_API = (
    "candidate_build_provenance",
    "candidate_build_provenance_payload",
    "candidate_build_provenance_digest",
    "parse_candidate_build_provenance",
    "expected_reserved_provenance_labels",
    "validate_reserved_provenance_labels",
    "validate_candidate_manifest_chain",
)
REQUIRED_DEPLOY_API = (
    "_parse_candidate_state_bytes",
    "_write_candidate_state",
)


class ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError(f"unexpected lookup: {key}")

    def __iter__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected iteration")

    def __len__(self) -> int:
        raise RuntimeError("unexpected length query")


def _load_provenance_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_provenance_contract_test",
        PROVENANCE_MODULE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_deploy_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_candidate_state_contract_test",
        DEPLOY_TOOL,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_provenance_module()
MISSING_PUBLIC_API = tuple(
    name for name in REQUIRED_PUBLIC_API if not callable(getattr(MODULE, name, None))
)
DEPLOY_MODULE = _load_deploy_module()
MISSING_DEPLOY_API = tuple(
    name
    for name in REQUIRED_DEPLOY_API
    if not callable(getattr(DEPLOY_MODULE, name, None))
)


@pytest.fixture(scope="module")
def provenance_module() -> ModuleType:
    if MISSING_PUBLIC_API:
        pytest.skip("P3.1 public provenance API is not implemented yet")
    return MODULE


@pytest.fixture(scope="module")
def deploy_module() -> ModuleType:
    if MISSING_DEPLOY_API:
        pytest.skip("P3.1 candidate-state API is not implemented yet")
    return DEPLOY_MODULE


def _source_manifest_payload(
    *,
    schema: int,
    source_sha: str = TARGET_SHA,
    image_id: str = TARGET_IMAGE_ID,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "approved_remote_ref": "refs/remotes/origin/main",
        "deployment_assets": {
            "schema": 1,
            "sha256": dict(DEPLOYMENT_ASSET_DIGESTS),
        },
        "docker_daemon_id": "local-daemon-id",
        "image_id": image_id,
        "image_reference": f"commerce-ops-desk:{source_sha}",
        "schema": schema,
        "source_sha": source_sha,
    }
    if schema == 3:
        payload["build"] = {"schema": 1, "kind": "source-build"}
    return payload


def _reuse_manifest_payload() -> dict[str, object]:
    payload = _source_manifest_payload(schema=3)
    payload["build"] = {
        "schema": 1,
        "kind": "verified-runtime-reuse",
        "reused_from": {
            "source_sha": DONOR_SHA,
            "image_reference": f"commerce-ops-desk:{DONOR_SHA}",
            "image_id": DONOR_IMAGE_ID,
            "manifest_sha256": "e" * 64,
        },
        "runtime_equivalence": {
            "schema": 1,
            "policy": "runtime-copy-inputs-v1+pip-timeout-300-v1",
            "donor_copy_inputs_sha256": COMBINED_RUNTIME_DIGEST,
            "target_copy_inputs_sha256": COMBINED_RUNTIME_DIGEST,
            "components": {
                name: {
                    "donor_sha256": digest,
                    "target_sha256": digest,
                }
                for name, digest in RUNTIME_COMPONENT_DIGESTS.items()
            },
            "dockerfile_transition": {
                "policy": "pip-timeout-300-v1",
                "from_sha256": (
                    "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
                ),
                "to_sha256": (
                    "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
                ),
            },
        },
    }
    return payload


def _canonical_manifest_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _load_manifest(module: ModuleType, case: str) -> object:
    if case == "schema2-source":
        raw = json.dumps(
            _source_manifest_payload(schema=2),
            separators=(",", ":"),
        ).encode("utf-8")
    elif case == "schema3-source":
        raw = _canonical_manifest_bytes(_source_manifest_payload(schema=3))
    elif case == "schema3-reuse":
        raw = _canonical_manifest_bytes(_reuse_manifest_payload())
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown manifest case: {case}")
    return module.parse_build_manifest_bytes(raw)


def _load_source_manifest(
    module: ModuleType,
    *,
    schema: int,
    source_sha: str = DONOR_SHA,
    image_id: str = DONOR_IMAGE_ID,
) -> object:
    payload = _source_manifest_payload(
        schema=schema,
        source_sha=source_sha,
        image_id=image_id,
    )
    raw = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if schema == 2
        else _canonical_manifest_bytes(payload)
    )
    return module.parse_build_manifest_bytes(raw)


def _load_reuse_target_for_donor(module: ModuleType, donor: object) -> object:
    payload = _reuse_manifest_payload()
    build = payload["build"]
    assert isinstance(build, dict)
    reused_from = build["reused_from"]
    assert isinstance(reused_from, dict)
    reused_from.update(
        {
            "source_sha": donor.source_sha,
            "image_reference": donor.image_reference,
            "image_id": donor.image_id,
            "manifest_sha256": donor.raw_sha256,
        }
    )
    return module.parse_build_manifest_bytes(_canonical_manifest_bytes(payload))


def _load_reuse_donor(module: ModuleType) -> object:
    payload = _reuse_manifest_payload()
    payload["source_sha"] = DONOR_SHA
    payload["image_reference"] = f"commerce-ops-desk:{DONOR_SHA}"
    payload["image_id"] = DONOR_IMAGE_ID
    build = payload["build"]
    assert isinstance(build, dict)
    reused_from = build["reused_from"]
    assert isinstance(reused_from, dict)
    reused_from.update(
        {
            "source_sha": GRANDDONOR_SHA,
            "image_reference": f"commerce-ops-desk:{GRANDDONOR_SHA}",
            "image_id": GRANDDONOR_IMAGE_ID,
            "manifest_sha256": "f" * 64,
        }
    )
    return module.parse_build_manifest_bytes(_canonical_manifest_bytes(payload))


def _source_candidate_payload(*, manifest_schema: int) -> dict[str, object]:
    manifest_digest = (
        SCHEMA2_MANIFEST_SHA256 if manifest_schema == 2 else SCHEMA3_MANIFEST_SHA256
    )
    return {
        "schema": 1,
        "kind": "source-build",
        "source_manifest_schema": manifest_schema,
        "source_manifest_sha256": manifest_digest,
    }


def _reuse_candidate_payload() -> dict[str, object]:
    build = copy.deepcopy(_reuse_manifest_payload()["build"])
    assert isinstance(build, dict)
    return {
        **build,
        "source_manifest_sha256": REUSE_MANIFEST_SHA256,
    }


def _expected_candidate_payload(case: str) -> dict[str, object]:
    if case == "schema2-source":
        return _source_candidate_payload(manifest_schema=2)
    if case == "schema3-source":
        return _source_candidate_payload(manifest_schema=3)
    if case == "schema3-reuse":
        return _reuse_candidate_payload()
    raise AssertionError(f"unknown candidate case: {case}")


def _candidate_provenance_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _candidate_provenance_sha256(payload: object) -> str:
    canonical = _candidate_provenance_bytes(payload)
    framed = (
        CANDIDATE_PROVENANCE_DOMAIN
        + len(canonical).to_bytes(8, byteorder="big", signed=False)
        + canonical
    )
    return hashlib.sha256(framed).hexdigest()


def _candidate_state_payload(
    case: str = "schema2-source",
    *,
    source_sha: str = TARGET_SHA,
) -> dict[str, object]:
    provenance = copy.deepcopy(_expected_candidate_payload(case))
    short_sha = source_sha[:12]
    project_name = f"commerce-ops-candidate-{short_sha}"
    return {
        "schema": 3,
        "source_sha": source_sha,
        "project_name": project_name,
        "container_name": f"app-commerce-ops-desk-candidate-{short_sha}",
        "container_id": "5" * 64,
        "image": f"commerce-ops-desk:{source_sha}",
        "image_id": TARGET_IMAGE_ID,
        "docker_daemon_id": "local-daemon-id",
        "candidate_port": 18_088,
        "candidate_data_dir": f"/srv/commerce-ops/data-candidate-{short_sha}",
        "candidate_data_device": 64_769,
        "candidate_data_inode": 55_451_027,
        "live_data_dir": "/srv/commerce-ops/data-live",
        "live_data_device": 64_769,
        "live_data_inode": 55_451_026,
        "network_name": f"{project_name}_default",
        "network_id": "6" * 64,
        "network_endpoint_id": "7" * 64,
        "runtime_sha256": "8" * 64,
        "trusted_proxy_cidrs": ["192.0.2.1/32"],
        "deployment_assets": {
            "schema": 1,
            "sha256": dict(DEPLOYMENT_ASSET_DIGESTS),
        },
        "build_provenance": provenance,
        "build_provenance_sha256": _candidate_provenance_sha256(provenance),
    }


def _candidate_state_filename(payload: Mapping[str, object]) -> str:
    source_sha = payload["source_sha"]
    assert isinstance(source_sha, str)
    return f"candidate-{source_sha[:12]}.json"


def _candidate_state_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _mapping_at_path(
    payload: dict[str, object],
    path: tuple[str, ...],
) -> dict[str, object]:
    current = payload
    for component in path:
        child = current[component]
        assert isinstance(child, dict)
        current = child
    return current


def _set_path(
    payload: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> None:
    assert path
    _mapping_at_path(payload, path[:-1])[path[-1]] = value


def _expected_reserved_labels(case: str) -> dict[str, str]:
    if case == "schema2-source":
        return {}
    if case == "schema3-source":
        return {BUILD_KIND_LABEL: "source-build"}
    if case == "schema3-reuse":
        return {
            BUILD_KIND_LABEL: "verified-runtime-reuse",
            REUSED_FROM_SOURCE_LABEL: DONOR_SHA,
            REUSED_FROM_IMAGE_ID_LABEL: DONOR_IMAGE_ID,
            RUNTIME_INPUTS_LABEL: COMBINED_RUNTIME_DIGEST,
        }
    raise AssertionError(f"unknown label case: {case}")


def test_p3_1_public_provenance_api_is_present() -> None:
    assert not MISSING_PUBLIC_API, "missing P3.1 public provenance API: " + ", ".join(
        MISSING_PUBLIC_API
    )


def test_p3_1_candidate_state_api_is_present() -> None:
    assert not MISSING_DEPLOY_API, "missing P3.1 candidate-state API: " + ", ".join(
        MISSING_DEPLOY_API
    )


@pytest.mark.parametrize("case", tuple(case for case, _, _ in GOLDEN_CASES))
def test_candidate_state_parser_accepts_schema3_for_every_provenance_arm(
    deploy_module: ModuleType,
    case: str,
) -> None:
    payload = _candidate_state_payload(case)

    loaded = deploy_module._parse_candidate_state_bytes(
        _candidate_state_bytes(payload),
        filename=_candidate_state_filename(payload),
    )

    assert deploy_module.CANDIDATE_STATE_SCHEMA == 3
    assert set(loaded) == CANDIDATE_STATE_FIELDS
    assert loaded == payload


def test_candidate_state_parser_rejects_schema2(
    deploy_module: ModuleType,
) -> None:
    payload = _candidate_state_payload()
    payload["schema"] = 2

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize("missing_field", sorted(CANDIDATE_STATE_FIELDS))
def test_candidate_state_parser_rejects_every_missing_field(
    deploy_module: ModuleType,
    missing_field: str,
) -> None:
    payload = _candidate_state_payload()
    filename = _candidate_state_filename(payload)
    del payload[missing_field]

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=filename,
        )


def test_candidate_state_parser_rejects_an_extra_field(
    deploy_module: ModuleType,
) -> None:
    payload = _candidate_state_payload()
    payload["unexpected"] = "field"

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize("duplicate_location", ("top-level", "provenance"))
def test_candidate_state_parser_rejects_duplicate_keys_at_every_depth(
    deploy_module: ModuleType,
    duplicate_location: str,
) -> None:
    payload = _candidate_state_payload()
    raw = _candidate_state_bytes(payload)
    if duplicate_location == "top-level":
        assert raw.endswith(b"}")
        raw = raw[:-1] + b',"schema":3}'
    else:
        marker = b'"kind":"source-build"'
        assert raw.count(marker) == 1
        raw = raw.replace(
            marker,
            b'"kind":"source-build","kind":"source-build"',
            1,
        )

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            raw,
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize(
    "nonfinite",
    (float("nan"), float("inf"), float("-inf")),
    ids=("nan", "infinity", "negative-infinity"),
)
def test_candidate_state_parser_rejects_nonfinite_numbers(
    deploy_module: ModuleType,
    nonfinite: float,
) -> None:
    payload = _candidate_state_payload()
    payload["candidate_port"] = nonfinite

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize(
    "field",
    (
        "schema",
        "candidate_port",
        "candidate_data_device",
        "candidate_data_inode",
        "live_data_device",
        "live_data_inode",
    ),
)
def test_candidate_state_parser_rejects_booleans_in_integer_fields(
    deploy_module: ModuleType,
    field: str,
) -> None:
    payload = _candidate_state_payload()
    payload[field] = True

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize(
    ("source_sha", "filename"),
    (
        (TARGET_SHA, "candidate-deadbeefdead.json"),
        (TARGET_SHA, f"candidate-{TARGET_SHA}.json"),
        (TARGET_SHA, f"candidate-{TARGET_SHA[:12]}.json.bak"),
        (TARGET_SHA, f"../candidate-{TARGET_SHA[:12]}.json"),
        ("1" * 40, f"candidate-{TARGET_SHA[:12]}.json"),
    ),
)
def test_candidate_state_parser_binds_filename_to_source_prefix(
    deploy_module: ModuleType,
    source_sha: str,
    filename: str,
) -> None:
    payload = _candidate_state_payload(source_sha=source_sha)

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=filename,
        )


def test_candidate_state_parser_rejects_malformed_provenance_with_matching_digest(
    deploy_module: ModuleType,
) -> None:
    payload = _candidate_state_payload()
    provenance = payload["build_provenance"]
    assert isinstance(provenance, dict)
    provenance["source_manifest_schema"] = 1
    payload["build_provenance_sha256"] = _candidate_provenance_sha256(provenance)

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


def test_candidate_state_parser_rejects_reuse_of_target_image_id(
    deploy_module: ModuleType,
) -> None:
    payload = _candidate_state_payload("schema3-reuse")
    payload["image_id"] = DONOR_IMAGE_ID

    with pytest.raises(deploy_module.DeploymentError, match="build provenance"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize(
    "persisted_digest",
    (
        "0" * 64,
        hashlib.sha256(SCHEMA2_SOURCE_CANONICAL).hexdigest(),
        hashlib.sha256(
            CANDIDATE_PROVENANCE_DOMAIN
            + len(SCHEMA2_SOURCE_CANONICAL).to_bytes(8, "little")
            + SCHEMA2_SOURCE_CANONICAL
        ).hexdigest(),
    ),
    ids=("zero", "unframed", "little-endian-length"),
)
def test_candidate_state_parser_rejects_wrong_provenance_digest(
    deploy_module: ModuleType,
    persisted_digest: str,
) -> None:
    payload = _candidate_state_payload()
    payload["build_provenance_sha256"] = persisted_digest

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._parse_candidate_state_bytes(
            _candidate_state_bytes(payload),
            filename=_candidate_state_filename(payload),
        )


@pytest.mark.parametrize("invalid_case", ("schema2", "wrong-digest"))
def test_candidate_state_writer_validates_before_private_io(
    deploy_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
) -> None:
    payload = _candidate_state_payload()
    if invalid_case == "schema2":
        payload["schema"] = 2
    else:
        payload["build_provenance_sha256"] = "0" * 64
    state_path = Path("/srv/commerce-ops/deploy-state") / _candidate_state_filename(
        payload
    )
    calls: list[tuple[Path, Mapping[str, object]]] = []
    monkeypatch.setattr(
        deploy_module,
        "_write_private_json",
        lambda path, value: calls.append((path, value)),
    )

    with pytest.raises(deploy_module.DeploymentError, match="candidate state"):
        deploy_module._write_candidate_state(state_path, payload)

    assert calls == []


def test_candidate_state_writer_delegates_validated_payload_once(
    deploy_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _candidate_state_payload()
    state_path = Path("/srv/commerce-ops/deploy-state") / _candidate_state_filename(
        payload
    )
    calls: list[tuple[Path, Mapping[str, object]]] = []
    monkeypatch.setattr(
        deploy_module,
        "_write_private_json",
        lambda path, value: calls.append((path, value)),
    )

    deploy_module._write_candidate_state(state_path, payload)

    assert calls == [(state_path, payload)]


def test_pinned_provenance_vectors_are_internally_consistent() -> None:
    expected_lengths = {
        "schema2-source": 153,
        "schema3-source": 153,
        "schema3-reuse": 1679,
    }
    for case, canonical, expected_digest in GOLDEN_CASES:
        framed = (
            CANDIDATE_PROVENANCE_DOMAIN
            + len(canonical).to_bytes(8, byteorder="big", signed=False)
            + canonical
        )
        assert len(canonical) == expected_lengths[case]
        assert hashlib.sha256(framed).hexdigest() == expected_digest


@pytest.mark.parametrize(
    ("case", "expected_manifest_sha256", "expected_fields"),
    (
        (
            "schema2-source",
            SCHEMA2_MANIFEST_SHA256,
            {
                "schema",
                "kind",
                "source_manifest_schema",
                "source_manifest_sha256",
            },
        ),
        (
            "schema3-source",
            SCHEMA3_MANIFEST_SHA256,
            {
                "schema",
                "kind",
                "source_manifest_schema",
                "source_manifest_sha256",
            },
        ),
        (
            "schema3-reuse",
            REUSE_MANIFEST_SHA256,
            {
                "schema",
                "kind",
                "source_manifest_sha256",
                "reused_from",
                "runtime_equivalence",
            },
        ),
    ),
)
def test_candidate_build_provenance_is_typed_for_every_manifest_arm(
    provenance_module: ModuleType,
    case: str,
    expected_manifest_sha256: str,
    expected_fields: set[str],
) -> None:
    manifest = _load_manifest(provenance_module, case)
    value = provenance_module.candidate_build_provenance(manifest)

    assert is_dataclass(value)
    assert not isinstance(value, Mapping)
    assert {field.name for field in fields(value)} == expected_fields
    assert value.schema == 1
    assert value.source_manifest_sha256 == expected_manifest_sha256
    if case.endswith("source"):
        assert value.kind == "source-build"
        assert value.source_manifest_schema == manifest.schema
    else:
        assert value.kind == "verified-runtime-reuse"
        assert value.reused_from == manifest.build.reused_from
        assert value.runtime_equivalence == manifest.build.runtime_equivalence
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.schema = 2


def test_legacy_and_schema3_source_provenance_share_one_typed_arm(
    provenance_module: ModuleType,
) -> None:
    legacy = provenance_module.candidate_build_provenance(
        _load_manifest(provenance_module, "schema2-source")
    )
    current = provenance_module.candidate_build_provenance(
        _load_manifest(provenance_module, "schema3-source")
    )

    assert type(legacy) is type(current)
    assert legacy.source_manifest_schema == 2
    assert current.source_manifest_schema == 3


@pytest.mark.parametrize(
    "invalid_manifest",
    (
        None,
        _source_manifest_payload(schema=2),
        _source_candidate_payload(manifest_schema=2),
        {BUILD_KIND_LABEL: "source-build"},
    ),
)
def test_candidate_build_provenance_rejects_non_typed_manifest_inputs(
    provenance_module: ModuleType,
    invalid_manifest: object,
) -> None:
    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.candidate_build_provenance(invalid_manifest)


def test_candidate_build_provenance_rebinds_typed_manifest_to_raw_bytes(
    provenance_module: ModuleType,
) -> None:
    manifest = _load_manifest(provenance_module, "schema3-source")
    forged = replace(manifest, source_sha=OTHER_DONOR_SHA)

    with pytest.raises(
        provenance_module.ProvenanceError,
        match="requires a loaded manifest",
    ):
        provenance_module.candidate_build_provenance(forged)


@pytest.mark.parametrize("case", tuple(case for case, _, _ in GOLDEN_CASES))
def test_candidate_provenance_payload_and_parser_round_trip_exactly(
    provenance_module: ModuleType,
    case: str,
) -> None:
    manifest = _load_manifest(provenance_module, case)
    generated = provenance_module.candidate_build_provenance(manifest)
    expected_payload = _expected_candidate_payload(case)

    assert provenance_module.candidate_build_provenance_payload(generated) == (
        expected_payload
    )
    parsed = provenance_module.parse_candidate_build_provenance(
        copy.deepcopy(expected_payload)
    )
    assert parsed == generated
    assert provenance_module.candidate_build_provenance_payload(parsed) == (
        expected_payload
    )


@pytest.mark.parametrize(("case", "canonical", "expected_digest"), GOLDEN_CASES)
def test_candidate_provenance_has_pinned_canonical_bytes_and_digest(
    provenance_module: ModuleType,
    case: str,
    canonical: bytes,
    expected_digest: str,
) -> None:
    manifest = _load_manifest(provenance_module, case)
    value = provenance_module.candidate_build_provenance(manifest)

    digest = provenance_module.candidate_build_provenance_digest(value)

    assert isinstance(digest, provenance_module.CanonicalDigest)
    assert digest.canonical_bytes == canonical
    assert digest.sha256 == expected_digest


def test_candidate_provenance_digest_changes_for_valid_tampering(
    provenance_module: ModuleType,
) -> None:
    original = provenance_module.parse_candidate_build_provenance(
        _source_candidate_payload(manifest_schema=3)
    )
    tampered_payload = _source_candidate_payload(manifest_schema=3)
    tampered_payload["source_manifest_sha256"] = "f" * 64
    tampered = provenance_module.parse_candidate_build_provenance(tampered_payload)

    original_digest = provenance_module.candidate_build_provenance_digest(original)
    tampered_digest = provenance_module.candidate_build_provenance_digest(tampered)

    assert original_digest.sha256 != tampered_digest.sha256
    assert original_digest.canonical_bytes != tampered_digest.canonical_bytes


def test_candidate_provenance_digest_cleans_hostile_mapping_errors(
    provenance_module: ModuleType,
) -> None:
    value = provenance_module.candidate_build_provenance(
        _load_manifest(provenance_module, "schema3-reuse")
    )
    hostile_equivalence = replace(
        value.runtime_equivalence,
        components=ExplodingMapping(),
    )
    hostile = replace(value, runtime_equivalence=hostile_equivalence)

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.candidate_build_provenance_digest(hostile)


@pytest.mark.parametrize(
    "persisted_digest",
    (
        "0" * 64,
        hashlib.sha256(SCHEMA3_SOURCE_CANONICAL).hexdigest(),
        hashlib.sha256(
            CANDIDATE_PROVENANCE_DOMAIN
            + len(SCHEMA3_SOURCE_CANONICAL).to_bytes(8, "little")
            + SCHEMA3_SOURCE_CANONICAL
        ).hexdigest(),
        hashlib.sha256(
            b"commerce-ops-desk:candidate-build-provenance:v2\0"
            + len(SCHEMA3_SOURCE_CANONICAL).to_bytes(8, "big")
            + SCHEMA3_SOURCE_CANONICAL
        ).hexdigest(),
    ),
)
def test_candidate_provenance_digest_exposes_every_persisted_mismatch(
    provenance_module: ModuleType,
    persisted_digest: str,
) -> None:
    value = provenance_module.parse_candidate_build_provenance(
        _source_candidate_payload(manifest_schema=3)
    )
    calculated = provenance_module.candidate_build_provenance_digest(value)

    assert calculated.sha256 != persisted_digest


@pytest.mark.parametrize(
    "field",
    ("schema", "kind", "source_manifest_schema", "source_manifest_sha256"),
)
def test_source_provenance_parser_rejects_every_missing_field(
    provenance_module: ModuleType,
    field: str,
) -> None:
    payload = _source_candidate_payload(manifest_schema=2)
    del payload[field]

    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(payload)


def test_source_provenance_parser_rejects_unknown_fields(
    provenance_module: ModuleType,
) -> None:
    payload = _source_candidate_payload(manifest_schema=2)
    payload["unexpected"] = "field"

    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(payload)


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    (
        (("schema",), True),
        (("schema",), 1.0),
        (("schema",), 2),
        (("kind",), True),
        (("kind",), "verified-runtime-reuse"),
        (("source_manifest_schema",), True),
        (("source_manifest_schema",), 2.0),
        (("source_manifest_schema",), 1),
        (("source_manifest_schema",), 4),
        (("source_manifest_sha256",), True),
        (("source_manifest_sha256",), "A" * 64),
        (("source_manifest_sha256",), "a" * 63),
    ),
)
def test_source_provenance_parser_rejects_invalid_scalars_and_boolean_integers(
    provenance_module: ModuleType,
    path: tuple[str, ...],
    invalid_value: object,
) -> None:
    payload = _source_candidate_payload(manifest_schema=2)
    _set_path(payload, path, invalid_value)

    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(payload)


@pytest.mark.parametrize(
    ("path", "field"),
    (
        ((), "schema"),
        ((), "kind"),
        ((), "source_manifest_sha256"),
        ((), "reused_from"),
        ((), "runtime_equivalence"),
        (("reused_from",), "source_sha"),
        (("reused_from",), "image_reference"),
        (("reused_from",), "image_id"),
        (("reused_from",), "manifest_sha256"),
        (("runtime_equivalence",), "schema"),
        (("runtime_equivalence",), "policy"),
        (("runtime_equivalence",), "donor_copy_inputs_sha256"),
        (("runtime_equivalence",), "target_copy_inputs_sha256"),
        (("runtime_equivalence",), "components"),
        (("runtime_equivalence",), "dockerfile_transition"),
        (("runtime_equivalence", "components"), "scripts"),
        (("runtime_equivalence", "components", "backend"), "donor_sha256"),
        (("runtime_equivalence", "components", "backend"), "target_sha256"),
        (("runtime_equivalence", "dockerfile_transition"), "policy"),
        (("runtime_equivalence", "dockerfile_transition"), "from_sha256"),
        (("runtime_equivalence", "dockerfile_transition"), "to_sha256"),
    ),
)
def test_reuse_provenance_parser_rejects_missing_fields_at_every_layer(
    provenance_module: ModuleType,
    path: tuple[str, ...],
    field: str,
) -> None:
    payload = _reuse_candidate_payload()
    del _mapping_at_path(payload, path)[field]

    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(payload)


@pytest.mark.parametrize(
    "path",
    (
        (),
        ("reused_from",),
        ("runtime_equivalence",),
        ("runtime_equivalence", "components"),
        ("runtime_equivalence", "components", "backend"),
        ("runtime_equivalence", "dockerfile_transition"),
    ),
)
def test_reuse_provenance_parser_rejects_unknown_fields_at_every_layer(
    provenance_module: ModuleType,
    path: tuple[str, ...],
) -> None:
    payload = _reuse_candidate_payload()
    _mapping_at_path(payload, path)["unexpected"] = "field"

    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(payload)


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    (
        (("schema",), True),
        (("schema",), 1.0),
        (("kind",), True),
        (("kind",), "source-build"),
        (("source_manifest_sha256",), True),
        (("source_manifest_sha256",), "A" * 64),
        (("reused_from", "source_sha"), True),
        (("reused_from", "source_sha"), "F" * 40),
        (("reused_from", "image_reference"), True),
        (("reused_from", "image_reference"), f"commerce-ops-desk:{TARGET_SHA}"),
        (("reused_from", "image_id"), True),
        (("reused_from", "image_id"), f"sha256:{'A' * 64}"),
        (("reused_from", "manifest_sha256"), True),
        (("reused_from", "manifest_sha256"), "e" * 63),
        (("runtime_equivalence", "schema"), True),
        (("runtime_equivalence", "schema"), 1.0),
        (("runtime_equivalence", "policy"), True),
        (("runtime_equivalence", "policy"), "runtime-copy-inputs-v2"),
        (("runtime_equivalence", "donor_copy_inputs_sha256"), True),
        (("runtime_equivalence", "donor_copy_inputs_sha256"), "A" * 64),
        (("runtime_equivalence", "target_copy_inputs_sha256"), "f" * 64),
        (
            ("runtime_equivalence", "components", "backend", "donor_sha256"),
            True,
        ),
        (
            ("runtime_equivalence", "components", "backend", "donor_sha256"),
            "A" * 64,
        ),
        (
            ("runtime_equivalence", "components", "backend", "target_sha256"),
            "f" * 64,
        ),
        (("runtime_equivalence", "dockerfile_transition", "policy"), True),
        (
            ("runtime_equivalence", "dockerfile_transition", "policy"),
            "pip-timeout-600-v1",
        ),
        (
            ("runtime_equivalence", "dockerfile_transition", "from_sha256"),
            "f" * 64,
        ),
        (
            ("runtime_equivalence", "dockerfile_transition", "to_sha256"),
            "f" * 64,
        ),
    ),
)
def test_reuse_provenance_parser_rejects_invalid_scalars_and_boolean_integers(
    provenance_module: ModuleType,
    path: tuple[str, ...],
    invalid_value: object,
) -> None:
    payload = _reuse_candidate_payload()
    _set_path(payload, path, invalid_value)

    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(payload)


@pytest.mark.parametrize("invalid_value", (None, True, 1, 1.0, "{}", [], ()))
def test_candidate_provenance_parser_rejects_non_mapping_values(
    provenance_module: ModuleType,
    invalid_value: object,
) -> None:
    with pytest.raises(provenance_module.ProvenanceError, match="provenance"):
        provenance_module.parse_candidate_build_provenance(invalid_value)


@pytest.mark.parametrize("case", tuple(case for case, _, _ in GOLDEN_CASES))
def test_expected_reserved_provenance_labels_follow_manifest_matrix(
    provenance_module: ModuleType,
    case: str,
) -> None:
    manifest = _load_manifest(provenance_module, case)
    labels = provenance_module.expected_reserved_provenance_labels(manifest)

    assert isinstance(labels, Mapping)
    assert dict(labels) == _expected_reserved_labels(case)
    assert all(key.startswith(RESERVED_LABEL_PREFIX) for key in labels)


@pytest.mark.parametrize("case", tuple(case for case, _, _ in GOLDEN_CASES))
def test_reserved_provenance_label_validator_allows_unrelated_labels(
    provenance_module: ModuleType,
    case: str,
) -> None:
    manifest = _load_manifest(provenance_module, case)
    labels = {
        "org.opencontainers.image.revision": TARGET_SHA,
        "io.github.zhang-zhenghao.commerce-opsx.build-kind": "unrelated",
        **_expected_reserved_labels(case),
    }

    assert (
        provenance_module.validate_reserved_provenance_labels(manifest, labels) is None
    )


@pytest.mark.parametrize(
    ("case", "extra_key", "extra_value"),
    (
        ("schema2-source", BUILD_KIND_LABEL, "source-build"),
        ("schema2-source", REUSED_FROM_SOURCE_LABEL, DONOR_SHA),
        ("schema2-source", REUSED_FROM_IMAGE_ID_LABEL, DONOR_IMAGE_ID),
        ("schema2-source", RUNTIME_INPUTS_LABEL, COMBINED_RUNTIME_DIGEST),
        ("schema3-source", REUSED_FROM_SOURCE_LABEL, DONOR_SHA),
        ("schema3-source", REUSED_FROM_IMAGE_ID_LABEL, DONOR_IMAGE_ID),
        ("schema3-source", RUNTIME_INPUTS_LABEL, COMBINED_RUNTIME_DIGEST),
    ),
)
def test_source_label_matrix_rejects_each_known_reserved_extra_key(
    provenance_module: ModuleType,
    case: str,
    extra_key: str,
    extra_value: str,
) -> None:
    manifest = _load_manifest(provenance_module, case)
    labels = {
        "org.opencontainers.image.revision": TARGET_SHA,
        **_expected_reserved_labels(case),
        extra_key: extra_value,
    }

    with pytest.raises(provenance_module.ProvenanceError, match="label"):
        provenance_module.validate_reserved_provenance_labels(manifest, labels)


@pytest.mark.parametrize(
    ("case", "mutation"),
    (
        ("schema2-source", "add-unknown-reserved"),
        ("schema3-source", "missing-build-kind"),
        ("schema3-source", "wrong-build-kind"),
        ("schema3-source", "boolean-build-kind"),
        ("schema3-source", "add-unknown-reserved"),
        ("schema3-reuse", "missing-build-kind"),
        ("schema3-reuse", "missing-reused-source"),
        ("schema3-reuse", "missing-reused-image"),
        ("schema3-reuse", "missing-runtime-inputs"),
        ("schema3-reuse", "wrong-build-kind"),
        ("schema3-reuse", "wrong-reused-source"),
        ("schema3-reuse", "wrong-reused-image"),
        ("schema3-reuse", "wrong-runtime-inputs"),
        ("schema3-reuse", "boolean-runtime-inputs"),
        ("schema3-reuse", "add-unknown-reserved"),
    ),
)
def test_reserved_provenance_label_validator_rejects_every_matrix_mismatch(
    provenance_module: ModuleType,
    case: str,
    mutation: str,
) -> None:
    manifest = _load_manifest(provenance_module, case)
    labels: dict[str, object] = {
        "org.opencontainers.image.revision": TARGET_SHA,
        **_expected_reserved_labels(case),
    }
    if mutation == "add-unknown-reserved":
        labels[f"{RESERVED_LABEL_PREFIX}unexpected"] = "value"
    elif mutation == "missing-build-kind":
        del labels[BUILD_KIND_LABEL]
    elif mutation == "missing-reused-source":
        del labels[REUSED_FROM_SOURCE_LABEL]
    elif mutation == "missing-reused-image":
        del labels[REUSED_FROM_IMAGE_ID_LABEL]
    elif mutation == "missing-runtime-inputs":
        del labels[RUNTIME_INPUTS_LABEL]
    elif mutation == "wrong-build-kind":
        labels[BUILD_KIND_LABEL] = "source-build-v2"
    elif mutation == "boolean-build-kind":
        labels[BUILD_KIND_LABEL] = True
    elif mutation == "wrong-reused-source":
        labels[REUSED_FROM_SOURCE_LABEL] = TARGET_SHA
    elif mutation == "wrong-reused-image":
        labels[REUSED_FROM_IMAGE_ID_LABEL] = TARGET_IMAGE_ID
    elif mutation == "wrong-runtime-inputs":
        labels[RUNTIME_INPUTS_LABEL] = "f" * 64
    elif mutation == "boolean-runtime-inputs":
        labels[RUNTIME_INPUTS_LABEL] = True
    else:  # pragma: no cover - parameter table guard
        raise AssertionError(f"unknown label mutation: {mutation}")

    with pytest.raises(provenance_module.ProvenanceError, match="label"):
        provenance_module.validate_reserved_provenance_labels(manifest, labels)


@pytest.mark.parametrize("invalid_labels", (None, True, [], "labels"))
def test_reserved_provenance_label_validator_rejects_non_mappings(
    provenance_module: ModuleType,
    invalid_labels: object,
) -> None:
    manifest = _load_manifest(provenance_module, "schema3-source")

    with pytest.raises(provenance_module.ProvenanceError, match="label"):
        provenance_module.validate_reserved_provenance_labels(
            manifest,
            invalid_labels,
        )


def test_reserved_provenance_label_validator_cleans_hostile_mapping_errors(
    provenance_module: ModuleType,
) -> None:
    manifest = _load_manifest(provenance_module, "schema3-source")

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.validate_reserved_provenance_labels(
            manifest,
            ExplodingMapping(),
        )


@pytest.mark.parametrize("case", ("schema2-source", "schema3-source"))
def test_candidate_manifest_chain_accepts_source_without_donor(
    provenance_module: ModuleType,
    case: str,
) -> None:
    target = _load_manifest(provenance_module, case)

    assert provenance_module.validate_candidate_manifest_chain(target, None) is None


@pytest.mark.parametrize("case", ("schema2-source", "schema3-source"))
def test_candidate_manifest_chain_rejects_source_with_a_donor(
    provenance_module: ModuleType,
    case: str,
) -> None:
    target = _load_manifest(provenance_module, case)
    donor = _load_source_manifest(provenance_module, schema=2)

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.validate_candidate_manifest_chain(target, donor)


@pytest.mark.parametrize("donor_schema", (2, 3))
def test_candidate_manifest_chain_accepts_reuse_with_source_donor(
    provenance_module: ModuleType,
    donor_schema: int,
) -> None:
    donor = _load_source_manifest(provenance_module, schema=donor_schema)
    target = _load_reuse_target_for_donor(provenance_module, donor)

    assert provenance_module.validate_candidate_manifest_chain(target, donor) is None


def test_candidate_manifest_chain_rejects_reuse_without_donor(
    provenance_module: ModuleType,
) -> None:
    target = _load_manifest(provenance_module, "schema3-reuse")

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.validate_candidate_manifest_chain(target, None)


def test_reuse_manifest_rejects_target_image_id_equal_to_donor(
    provenance_module: ModuleType,
) -> None:
    payload = _reuse_manifest_payload()
    payload["image_id"] = DONOR_IMAGE_ID

    with pytest.raises(provenance_module.ProvenanceError, match="identity"):
        provenance_module.parse_build_manifest_bytes(_canonical_manifest_bytes(payload))


def test_candidate_manifest_chain_rejects_reuse_of_reuse(
    provenance_module: ModuleType,
) -> None:
    donor = _load_reuse_donor(provenance_module)
    target = _load_reuse_target_for_donor(provenance_module, donor)

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.validate_candidate_manifest_chain(target, donor)


def test_candidate_manifest_chain_rejects_donor_identity_mismatch(
    provenance_module: ModuleType,
) -> None:
    donor = _load_source_manifest(
        provenance_module,
        schema=2,
        source_sha=OTHER_DONOR_SHA,
        image_id=OTHER_DONOR_IMAGE_ID,
    )

    target_payload = _reuse_manifest_payload()
    build = target_payload["build"]
    assert isinstance(build, dict)
    reused_from = build["reused_from"]
    assert isinstance(reused_from, dict)
    reused_from["manifest_sha256"] = donor.raw_sha256
    target = provenance_module.parse_build_manifest_bytes(
        _canonical_manifest_bytes(target_payload)
    )

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.validate_candidate_manifest_chain(target, donor)


def test_candidate_manifest_chain_rejects_donor_manifest_digest_mismatch(
    provenance_module: ModuleType,
) -> None:
    donor = _load_source_manifest(provenance_module, schema=2)
    target = _load_manifest(provenance_module, "schema3-reuse")
    assert target.build.reused_from.manifest_sha256 != donor.raw_sha256

    with pytest.raises(provenance_module.ProvenanceError):
        provenance_module.validate_candidate_manifest_chain(target, donor)


def test_typed_manifest_arm_distinguishes_a_forbidden_reuse_donor() -> None:
    """P3.3 can reject reuse-of-reuse without trusting mutable image labels."""

    source2 = _load_manifest(MODULE, "schema2-source")
    source3 = _load_manifest(MODULE, "schema3-source")
    reuse = _load_manifest(MODULE, "schema3-reuse")

    assert isinstance(source2.build, MODULE.SourceBuild)
    assert isinstance(source3.build, MODULE.SourceBuild)
    assert not isinstance(reuse.build, MODULE.SourceBuild)
