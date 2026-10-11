"""Contracts for shared verified build and deployment provenance helpers."""

# Nested contexts make descriptor and lock ownership explicit in these tests.
# ruff: noqa: SIM117

from __future__ import annotations

import errno
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_MODULE = PRODUCT_ROOT / "deploy" / "workstation" / "verified_provenance.py"
ENTRYPOINTS = (
    PRODUCT_ROOT / "deploy" / "workstation" / "build_verified_image.py",
    PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py",
)
TARGET_SHA = "0123456789abcdef0123456789abcdef01234567"
DONOR_SHA = "fedcba9876543210fedcba9876543210fedcba98"
TARGET_IMAGE_ID = f"sha256:{'1' * 64}"
DONOR_IMAGE_ID = f"sha256:{'2' * 64}"
RUNTIME_COMPONENT_DIGESTS = {
    "backend": "a" * 64,
    "dockerignore": "b" * 64,
    "frontend": "c" * 64,
    "scripts": "d" * 64,
}
COMBINED_RUNTIME_DIGEST = (
    "b3b1f9868a0e8ca6fb608f7752d057a280e381d45925e831b2c00f70d0b5713a"
)
DEPLOYMENT_ASSET_DIGESTS = {
    "deploy/workstation/compose.yaml": "a" * 64,
    "deploy/workstation/commerce-ops-desk.Caddyfile.template": "b" * 64,
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template": "c" * 64,
}


def source_manifest_payload(*, schema: int = 3) -> dict[str, object]:
    payload: dict[str, object] = {
        "approved_remote_ref": "refs/remotes/origin/main",
        "deployment_assets": {
            "schema": 1,
            "sha256": dict(DEPLOYMENT_ASSET_DIGESTS),
        },
        "docker_daemon_id": "local-daemon-id",
        "image_id": TARGET_IMAGE_ID,
        "image_reference": f"commerce-ops-desk:{TARGET_SHA}",
        "schema": schema,
        "source_sha": TARGET_SHA,
    }
    if schema == 3:
        payload["build"] = {"schema": 1, "kind": "source-build"}
    return payload


def reuse_manifest_payload() -> dict[str, object]:
    payload = source_manifest_payload()
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


def canonical_test_bytes(payload: object) -> bytes:
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


def create_canonical_state_tree(root: Path) -> tuple[Path, Path]:
    account_home = root / "account-home"
    state = account_home / "apps" / "commerce-ops-desk" / "deploy-state"
    state.mkdir(mode=0o700, parents=True)
    account_home.chmod(0o700)
    (account_home / "apps").chmod(0o700)
    (account_home / "apps" / "commerce-ops-desk").chmod(0o700)
    state.chmod(0o700)
    return account_home, state


def set_passwd_home(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    account_home: Path,
) -> None:
    monkeypatch.setattr(
        module.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(account_home)),
    )


def _load_entrypoint(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_provenance_module() -> ModuleType:
    return _load_entrypoint(
        PROVENANCE_MODULE,
        "commerce_ops_verified_provenance_contract_test",
    )


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.name)
def test_entrypoint_loads_the_exact_sibling_provenance_module(
    entrypoint: Path,
) -> None:
    module = _load_entrypoint(
        entrypoint,
        f"commerce_ops_provenance_entrypoint_{entrypoint.stem}",
    )

    assert PROVENANCE_MODULE.is_file()
    assert Path(module._PROVENANCE.__file__).resolve(strict=True) == PROVENANCE_MODULE
    assert sys.modules[module._PROVENANCE.__name__] is module._PROVENANCE


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.name)
def test_entrypoint_loads_sibling_provenance_under_isolated_python(
    entrypoint: Path,
    tmp_path: Path,
) -> None:
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "verified_provenance.py").write_text(
        "raise RuntimeError('attacker module executed')\n",
        encoding="utf-8",
    )
    probe = """
import importlib.util
from pathlib import Path
import sys

entrypoint = Path(sys.argv[1])
expected = entrypoint.with_name("verified_provenance.py").resolve(strict=True)
spec = importlib.util.spec_from_file_location("isolated_provenance_entrypoint", entrypoint)
if spec is None or spec.loader is None:
    raise SystemExit(91)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if Path(module._PROVENANCE.__file__).resolve(strict=True) != expected:
    raise SystemExit(92)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(attacker)

    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", probe, str(entrypoint)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.name)
def test_entrypoint_loads_sibling_despite_hostile_pythonpath(
    entrypoint: Path,
    tmp_path: Path,
) -> None:
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "verified_provenance.py").write_text(
        "raise RuntimeError('SECRET-ATTACKER-MODULE')\n",
        encoding="utf-8",
    )
    probe = """
import importlib.util
from pathlib import Path
import sys

entrypoint = Path(sys.argv[1])
expected = entrypoint.with_name("verified_provenance.py").resolve(strict=True)
spec = importlib.util.spec_from_file_location("hostile_path_provenance_entrypoint", entrypoint)
if spec is None or spec.loader is None:
    raise SystemExit(91)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if Path(module._PROVENANCE.__file__).resolve(strict=True) != expected:
    raise SystemExit(92)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(attacker)

    result = subprocess.run(
        ["/usr/bin/python3", "-c", probe, str(entrypoint)],
        cwd=attacker,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SECRET-ATTACKER-MODULE" not in result.stdout + result.stderr


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.name)
def test_entrypoint_help_runs_directly_under_isolated_python(
    entrypoint: Path,
    tmp_path: Path,
) -> None:
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "verified_provenance.py").write_text(
        "raise RuntimeError('attacker module executed')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(attacker)

    result = subprocess.run(
        ["/usr/bin/python3", "-I", str(entrypoint), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize(
    "raw",
    (
        b'{"schema":1,"schema":2}',
        b'{"outer":{"value":1,"value":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e1000000}',
        b'{"value":-1e1000000}',
        b'\xef\xbb\xbf{"schema":1}',
        b"\xff",
    ),
)
def test_strict_json_rejects_ambiguous_or_nonstandard_input(raw: bytes) -> None:
    module = load_provenance_module()

    with pytest.raises(module.ProvenanceError, match="JSON data is invalid") as error:
        module.strict_json_loads(raw)

    rejected_text = raw.decode("utf-8", errors="ignore")
    if rejected_text:
        assert rejected_text not in str(error.value)


def test_strict_json_accepts_one_unambiguous_document() -> None:
    module = load_provenance_module()

    assert module.strict_json_loads(
        b'{"items":[true,false,null,1,1.5,"text"],"schema":1}'
    ) == {
        "items": [True, False, None, 1, 1.5, "text"],
        "schema": 1,
    }


def test_strict_json_sanitizes_excessive_nesting() -> None:
    module = load_provenance_module()
    raw = b"[" * 10_000 + b'"SECRET-DEEP-JSON-CANARY"' + b"]" * 10_000

    with pytest.raises(
        module.ProvenanceError, match="^JSON data is invalid$"
    ) as caught:
        module.strict_json_loads(raw)

    assert "SECRET-DEEP-JSON-CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    ("serializer_name", "expected_message"),
    (
        ("canonical_manifest_bytes", "manifest data is not serializable"),
        ("canonical_json_bytes", "provenance data is not serializable"),
    ),
)
def test_canonical_serializers_sanitize_excessive_nesting(
    serializer_name: str,
    expected_message: str,
) -> None:
    module = load_provenance_module()
    payload: object = "SECRET-DEEP-JSON-CANARY"
    for _ in range(10_000):
        payload = [payload]

    serializer = getattr(module, serializer_name)
    with pytest.raises(module.ProvenanceError, match=f"^{expected_message}$") as caught:
        serializer(payload)

    assert "SECRET-DEEP-JSON-CANARY" not in str(caught.value)


def test_canonical_manifest_bytes_match_the_pinned_source_manifest() -> None:
    module = load_provenance_module()
    expected = b"""{
  "approved_remote_ref": "refs/remotes/origin/main",
  "build": {
    "kind": "source-build",
    "schema": 1
  },
  "deployment_assets": {
    "schema": 1,
    "sha256": {
      "deploy/workstation/commerce-ops-desk.Caddyfile.template": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "deploy/workstation/compose.yaml": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  },
  "docker_daemon_id": "local-daemon-id",
  "image_id": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "image_reference": "commerce-ops-desk:0123456789abcdef0123456789abcdef01234567",
  "schema": 3,
  "source_sha": "0123456789abcdef0123456789abcdef01234567"
}
"""

    assert module.canonical_manifest_bytes(source_manifest_payload()) == expected


def test_canonical_json_bytes_match_pinned_candidate_provenance() -> None:
    module = load_provenance_module()
    payload = {
        "schema": 1,
        "kind": "source-build",
        "source_manifest_schema": 2,
        "source_manifest_sha256": "d" * 64,
    }
    expected = (
        b'{"kind":"source-build","schema":1,"source_manifest_schema":2,'
        b'"source_manifest_sha256":"dddddddddddddddddddddddddddddddddddddddddddd'
        b'dddddddddddddddddddd"}'
    )

    assert module.canonical_json_bytes(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (b"", "c89277a1f863ceec1dc7c07ad7b1390c1ee975714dd86ec39757399a6c926671"),
        (b"a", "b99e695d3b33d7833292c614f9d67c9aeed077a83978b5b737f22ef70cc0af73"),
        (
            b'{"a":1,"b":2}',
            "7648ae0cb4ada22ac98966fc0658bfb4415bdf3f3288454c69be90d2fcc3272c",
        ),
    ),
)
def test_length_framed_sha256_matches_pinned_vectors(
    payload: bytes,
    expected: str,
) -> None:
    module = load_provenance_module()
    domain = b"commerce-ops-desk:runtime-copy-input-component:v1\0"

    assert module.length_framed_sha256(domain, payload) == expected


def test_parse_manifest_accepts_noncanonical_schema2_as_typed_legacy_source() -> None:
    module = load_provenance_module()
    raw = json.dumps(
        source_manifest_payload(schema=2),
        separators=(",", ":"),
    ).encode("utf-8")

    loaded = module.parse_build_manifest_bytes(raw)

    assert loaded.schema == 2
    assert isinstance(loaded.build, module.SourceBuild)
    assert loaded.build.schema == 1
    assert loaded.build.kind == "source-build"
    assert loaded.source_sha == TARGET_SHA
    assert loaded.image_reference == f"commerce-ops-desk:{TARGET_SHA}"
    assert loaded.image_id == TARGET_IMAGE_ID
    assert loaded.approved_remote_ref == "refs/remotes/origin/main"
    assert loaded.docker_daemon_id == "local-daemon-id"
    assert dict(loaded.deployment_assets.sha256) == DEPLOYMENT_ASSET_DIGESTS
    with pytest.raises(TypeError):
        loaded.deployment_assets.sha256["unexpected"] = "f" * 64
    assert loaded.raw_bytes == raw
    assert (
        loaded.raw_sha256
        == "b4d76652d8c9b149768f72b75ed2219458a3b966ed41d2f5795acaeddd37a4d3"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("build", {"schema": 1, "kind": "verified-runtime-reuse"}),
        ("unexpected", "value"),
    ),
)
def test_schema2_manifest_rejects_every_extra_top_level_field(
    field: str,
    value: object,
) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload(schema=2)
    payload[field] = value
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    with pytest.raises(module.ProvenanceError, match="build manifest schema"):
        module.parse_build_manifest_bytes(raw)


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown-build-kind",
        "missing-build-field",
        "extra-build-field",
        "top-schema-bool",
        "top-schema-float",
        "build-schema-bool",
        "build-schema-float",
    ),
)
def test_schema3_source_manifest_rejects_unknown_or_inexact_build_schema(
    mutation: str,
) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload()
    build = payload["build"]
    assert isinstance(build, dict)
    if mutation == "unknown-build-kind":
        build["kind"] = "unreviewed-build-kind"
    elif mutation == "missing-build-field":
        build.pop("schema")
    elif mutation == "extra-build-field":
        build["unexpected"] = "field"
    elif mutation == "top-schema-bool":
        payload["schema"] = True
    elif mutation == "top-schema-float":
        payload["schema"] = 3.0
    elif mutation == "build-schema-bool":
        build["schema"] = True
    else:
        build["schema"] = 1.0

    with pytest.raises(module.ProvenanceError, match="build manifest"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


def test_parse_manifest_accepts_canonical_schema3_source_as_typed_source() -> None:
    module = load_provenance_module()
    raw = canonical_test_bytes(source_manifest_payload())

    loaded = module.parse_build_manifest_bytes(raw)

    assert loaded.schema == 3
    assert isinstance(loaded.build, module.SourceBuild)
    assert loaded.build.schema == 1
    assert loaded.build.kind == "source-build"
    assert loaded.source_sha == TARGET_SHA
    assert loaded.image_reference == f"commerce-ops-desk:{TARGET_SHA}"
    assert loaded.raw_bytes == raw
    assert (
        loaded.raw_sha256
        == "b608ed33fda2a997299fe299b159f87f9d3b9d659a1b80725d68261f7b9cce58"
    )


@pytest.mark.parametrize(
    "mutation",
    ("assets-extra-field", "asset-path-extra", "asset-path-missing"),
)
def test_manifest_rejects_unknown_deployment_asset_fields(mutation: str) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload()
    assets = payload["deployment_assets"]
    assert isinstance(assets, dict)
    digests = assets["sha256"]
    assert isinstance(digests, dict)
    if mutation == "assets-extra-field":
        assets["unexpected"] = "value"
    elif mutation == "asset-path-extra":
        digests["deploy/workstation/unexpected.yaml"] = "d" * 64
    else:
        del digests["deploy/workstation/compose.yaml"]

    with pytest.raises(module.ProvenanceError, match="build manifest schema"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize("invalid_schema", (True, 1.0))
def test_manifest_rejects_boolean_or_float_deployment_asset_schema(
    invalid_schema: object,
) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload()
    assets = payload["deployment_assets"]
    assert isinstance(assets, dict)
    assets["schema"] = invalid_schema

    with pytest.raises(module.ProvenanceError, match="build manifest schema"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("source_sha", TARGET_SHA.upper()),
        ("source_sha", True),
        ("image_reference", "commerce-ops-desk:wrong"),
        ("image_id", f"sha256:{'A' * 64}"),
        ("approved_remote_ref", "refs/remotes/origin/../main"),
        ("approved_remote_ref", "refs/remotes/origin/.hidden"),
        ("approved_remote_ref", "refs/remotes/origin/foo.lock/bar"),
        ("approved_remote_ref", "refs/remotes/origin/main\x7f"),
        ("approved_remote_ref", True),
        ("docker_daemon_id", ""),
        ("docker_daemon_id", "daemon\nidentifier"),
    ),
)
def test_source_manifest_rejects_invalid_identity_scalars(
    field: str,
    invalid_value: object,
) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload()
    payload[field] = invalid_value

    with pytest.raises(module.ProvenanceError, match="build manifest"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize("invalid_digest", ("a" * 63, "A" * 64, True))
def test_source_manifest_rejects_invalid_deployment_asset_digest(
    invalid_digest: object,
) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload()
    assets = payload["deployment_assets"]
    assert isinstance(assets, dict)
    digests = assets["sha256"]
    assert isinstance(digests, dict)
    digests["deploy/workstation/compose.yaml"] = invalid_digest

    with pytest.raises(module.ProvenanceError, match="build manifest"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize(
    "encoding",
    ("reordered", "compact", "missing-newline", "extra-newline", "indent-four"),
)
def test_schema3_manifest_requires_exact_canonical_bytes(encoding: str) -> None:
    module = load_provenance_module()
    payload = source_manifest_payload()
    canonical = canonical_test_bytes(payload)
    if encoding == "reordered":
        raw = (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")
    elif encoding == "compact":
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    elif encoding == "missing-newline":
        raw = canonical.removesuffix(b"\n")
    elif encoding == "extra-newline":
        raw = canonical + b"\n"
    else:
        raw = (json.dumps(payload, indent=4, sort_keys=True) + "\n").encode("utf-8")
    assert raw != canonical

    with pytest.raises(module.ProvenanceError, match="not canonical"):
        module.parse_build_manifest_bytes(raw)


def test_parse_manifest_accepts_canonical_schema3_reuse_as_typed_reuse() -> None:
    module = load_provenance_module()
    raw = canonical_test_bytes(reuse_manifest_payload())

    loaded = module.parse_build_manifest_bytes(raw)

    assert loaded.schema == 3
    assert isinstance(loaded.build, module.VerifiedRuntimeReuse)
    assert loaded.build.schema == 1
    assert loaded.build.kind == "verified-runtime-reuse"
    assert loaded.build.reused_from.source_sha == DONOR_SHA
    assert loaded.build.reused_from.image_reference == f"commerce-ops-desk:{DONOR_SHA}"
    assert loaded.build.reused_from.image_id == DONOR_IMAGE_ID
    assert loaded.build.reused_from.manifest_sha256 == "e" * 64
    equivalence = loaded.build.runtime_equivalence
    assert equivalence.schema == 1
    assert equivalence.policy == "runtime-copy-inputs-v1+pip-timeout-300-v1"
    assert equivalence.donor_copy_inputs_sha256 == COMBINED_RUNTIME_DIGEST
    assert equivalence.target_copy_inputs_sha256 == COMBINED_RUNTIME_DIGEST
    assert set(equivalence.components) == set(RUNTIME_COMPONENT_DIGESTS)
    with pytest.raises(TypeError):
        equivalence.components["unexpected"] = module.DigestPair("a" * 64, "a" * 64)
    assert equivalence.dockerfile_transition.policy == "pip-timeout-300-v1"
    assert loaded.raw_bytes == raw


@pytest.mark.parametrize(
    "mutation",
    (
        "build-extra",
        "reused-from-extra",
        "equivalence-extra",
        "components-extra",
        "components-missing",
        "pair-extra",
        "transition-extra",
    ),
)
def test_reuse_manifest_rejects_unknown_fields_at_every_nested_layer(
    mutation: str,
) -> None:
    module = load_provenance_module()
    payload = reuse_manifest_payload()
    build = payload["build"]
    assert isinstance(build, dict)
    reused = build["reused_from"]
    equivalence = build["runtime_equivalence"]
    assert isinstance(reused, dict)
    assert isinstance(equivalence, dict)
    components = equivalence["components"]
    transition = equivalence["dockerfile_transition"]
    assert isinstance(components, dict)
    assert isinstance(transition, dict)
    if mutation == "build-extra":
        build["unexpected"] = "value"
    elif mutation == "reused-from-extra":
        reused["unexpected"] = "value"
    elif mutation == "equivalence-extra":
        equivalence["unexpected"] = "value"
    elif mutation == "components-extra":
        components["unexpected"] = {
            "donor_sha256": "f" * 64,
            "target_sha256": "f" * 64,
        }
    elif mutation == "components-missing":
        del components["scripts"]
    elif mutation == "pair-extra":
        pair = components["backend"]
        assert isinstance(pair, dict)
        pair["unexpected"] = "value"
    else:
        transition["unexpected"] = "value"

    with pytest.raises(module.ProvenanceError, match="build manifest schema"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize("location", ("build", "runtime-equivalence"))
@pytest.mark.parametrize("invalid_schema", (True, 2, 1.0))
def test_reuse_manifest_rejects_invalid_nested_schema_discriminators(
    location: str,
    invalid_schema: object,
) -> None:
    module = load_provenance_module()
    payload = reuse_manifest_payload()
    build = payload["build"]
    assert isinstance(build, dict)
    target = build if location == "build" else build["runtime_equivalence"]
    assert isinstance(target, dict)
    target["schema"] = invalid_schema

    with pytest.raises(module.ProvenanceError, match="build manifest schema"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize(
    "mutation",
    (
        "donor-equals-target",
        "donor-sha-uppercase",
        "donor-reference-mismatch",
        "donor-image-id-invalid",
        "donor-manifest-digest-invalid",
    ),
)
def test_reuse_manifest_rejects_unbound_or_invalid_donor_identity(
    mutation: str,
) -> None:
    module = load_provenance_module()
    payload = reuse_manifest_payload()
    build = payload["build"]
    assert isinstance(build, dict)
    reused = build["reused_from"]
    assert isinstance(reused, dict)
    if mutation == "donor-equals-target":
        reused["source_sha"] = TARGET_SHA
        reused["image_reference"] = f"commerce-ops-desk:{TARGET_SHA}"
    elif mutation == "donor-sha-uppercase":
        reused["source_sha"] = DONOR_SHA.upper()
    elif mutation == "donor-reference-mismatch":
        reused["image_reference"] = f"commerce-ops-desk:{TARGET_SHA}"
    elif mutation == "donor-image-id-invalid":
        reused["image_id"] = f"sha256:{'A' * 64}"
    else:
        reused["manifest_sha256"] = "e" * 63

    with pytest.raises(module.ProvenanceError, match="build manifest"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


@pytest.mark.parametrize(
    "mutation",
    (
        "combined-mismatch",
        "combined-invalid",
        "component-mismatch",
        "component-invalid",
        "runtime-policy",
        "transition-policy",
        "transition-from",
        "transition-to",
    ),
)
def test_reuse_manifest_rejects_unapproved_or_unequal_runtime_evidence(
    mutation: str,
) -> None:
    module = load_provenance_module()
    payload = reuse_manifest_payload()
    build = payload["build"]
    assert isinstance(build, dict)
    equivalence = build["runtime_equivalence"]
    assert isinstance(equivalence, dict)
    components = equivalence["components"]
    transition = equivalence["dockerfile_transition"]
    assert isinstance(components, dict)
    assert isinstance(transition, dict)
    if mutation == "combined-mismatch":
        equivalence["target_copy_inputs_sha256"] = "f" * 64
    elif mutation == "combined-invalid":
        equivalence["donor_copy_inputs_sha256"] = True
        equivalence["target_copy_inputs_sha256"] = True
    elif mutation == "component-mismatch":
        pair = components["backend"]
        assert isinstance(pair, dict)
        pair["target_sha256"] = "f" * 64
    elif mutation == "component-invalid":
        pair = components["backend"]
        assert isinstance(pair, dict)
        pair["donor_sha256"] = "A" * 64
        pair["target_sha256"] = "A" * 64
    elif mutation == "runtime-policy":
        equivalence["policy"] = "runtime-copy-inputs-v2"
    elif mutation == "transition-policy":
        transition["policy"] = "pip-timeout-600-v1"
    elif mutation == "transition-from":
        transition["from_sha256"] = "f" * 64
    else:
        transition["to_sha256"] = "f" * 64

    with pytest.raises(module.ProvenanceError, match="build manifest"):
        module.parse_build_manifest_bytes(canonical_test_bytes(payload))


def test_state_directory_uses_passwd_home_and_ignores_hostile_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, expected_state = create_canonical_state_tree(tmp_path / "trusted")
    hostile_home, _ = create_canonical_state_tree(tmp_path / "hostile")
    set_passwd_home(module, monkeypatch, account_home)
    monkeypatch.setenv("HOME", str(hostile_home))

    with module.open_canonical_state_directory() as state:
        assert state.path == expected_state
        assert state.effective_uid == os.geteuid()
        assert state.descriptor >= 0
        state.revalidate()


def test_missing_canonical_state_is_rejected_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home = tmp_path / "account-home"
    account_home.mkdir(mode=0o700)
    set_passwd_home(module, monkeypatch, account_home)
    expected_state = account_home / "apps" / "commerce-ops-desk" / "deploy-state"

    with pytest.raises(module.ProvenanceError, match="state directory"):
        module.open_canonical_state_directory()

    assert not expected_state.exists()


@pytest.mark.parametrize(
    "unsafe_component",
    (
        "home-writable",
        "apps-writable",
        "app-writable",
        "state-not-0700",
        "apps-symlink",
        "state-symlink",
    ),
)
def test_state_directory_rejects_unsafe_or_symlinked_components(
    unsafe_component: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state = create_canonical_state_tree(tmp_path)
    apps = account_home / "apps"
    app = apps / "commerce-ops-desk"
    if unsafe_component == "home-writable":
        account_home.chmod(0o720)
    elif unsafe_component == "apps-writable":
        apps.chmod(0o702)
    elif unsafe_component == "app-writable":
        app.chmod(0o770)
    elif unsafe_component == "state-not-0700":
        state.chmod(0o750)
    elif unsafe_component == "apps-symlink":
        state.rmdir()
        app.rmdir()
        apps.rmdir()
        real_apps = tmp_path / "real-apps"
        real_state = real_apps / "commerce-ops-desk" / "deploy-state"
        real_state.mkdir(mode=0o700, parents=True)
        real_apps.chmod(0o700)
        (real_apps / "commerce-ops-desk").chmod(0o700)
        apps.symlink_to(real_apps, target_is_directory=True)
    else:
        state.rmdir()
        real_state = tmp_path / "real-state"
        real_state.mkdir(mode=0o700)
        state.symlink_to(real_state, target_is_directory=True)
    set_passwd_home(module, monkeypatch, account_home)

    with pytest.raises(module.ProvenanceError, match="state directory"):
        module.open_canonical_state_directory()


def test_state_directory_rejects_a_symlinked_passwd_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    real_home, _ = create_canonical_state_tree(tmp_path / "real")
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    set_passwd_home(module, monkeypatch, linked_home)

    with pytest.raises(module.ProvenanceError, match="state directory"):
        module.open_canonical_state_directory()


def test_state_directory_sanitizes_a_passwd_home_symlink_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    loop = tmp_path / "SECRET-SYMLINK-LOOP-CANARY"
    loop.symlink_to(loop, target_is_directory=True)
    set_passwd_home(module, monkeypatch, loop)

    with pytest.raises(module.ProvenanceError, match="state directory") as caught:
        module.open_canonical_state_directory()

    assert "SECRET-SYMLINK-LOOP-CANARY" not in str(caught.value)


def test_state_directory_revalidation_sanitizes_a_home_symlink_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)

    with module.open_canonical_state_directory() as state:
        displaced = tmp_path / "displaced-home"
        account_home.rename(displaced)
        account_home.symlink_to(account_home, target_is_directory=True)
        with pytest.raises(module.ProvenanceError, match="identity changed") as caught:
            state.revalidate()

    assert str(account_home) not in str(caught.value)


@pytest.mark.parametrize("root_kind", ("state", "checkout"))
def test_canonical_roots_sanitize_embedded_nul_paths(
    root_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    supplied = Path(f"{tmp_path}/SECRET-NUL-PATH-CANARY\x00")
    if root_kind == "state":
        set_passwd_home(module, monkeypatch, supplied)

    with pytest.raises(
        module.ProvenanceError, match="state directory|checkout root"
    ) as caught:
        if root_kind == "state":
            module.open_canonical_state_directory()
        else:
            module.open_canonical_checkout_root(supplied)

    assert "SECRET-NUL-PATH-CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    "relative_path",
    tuple(DEPLOYMENT_ASSET_DIGESTS),
    ids=lambda path: Path(path).name,
)
def test_checkout_root_retains_identity_and_reads_only_approved_assets(
    relative_path: str,
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    asset = checkout / relative_path
    asset.parent.mkdir(parents=True)
    checkout.chmod(0o755)
    (checkout / "deploy").chmod(0o755)
    (checkout / "deploy" / "workstation").chmod(0o755)
    asset.write_text("services: {}\n", encoding="utf-8")
    asset.chmod(0o644)

    with module.open_canonical_checkout_root(checkout) as root:
        assert root.path == checkout
        assert root.effective_uid == os.geteuid()
        assert root.descriptor >= 0
        root.revalidate()
        with module.safe_read_deployment_asset(root, relative_path) as opened:
            assert opened.data == b"services: {}\n"
            assert opened.text == "services: {}\n"
            opened.revalidate()


@pytest.mark.parametrize("unsafe_shape", ("relative", "symlink", "writable"))
def test_checkout_root_rejects_noncanonical_or_unsafe_roots(
    unsafe_shape: str,
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    checkout.mkdir(mode=0o755)
    supplied = checkout
    if unsafe_shape == "relative":
        supplied = Path("checkout")
    elif unsafe_shape == "symlink":
        linked = tmp_path / "linked-checkout"
        linked.symlink_to(checkout, target_is_directory=True)
        supplied = linked
    else:
        checkout.chmod(0o775)

    with pytest.raises(module.ProvenanceError, match="checkout root"):
        module.open_canonical_checkout_root(supplied)


def test_checkout_root_revalidation_rejects_path_replacement(tmp_path: Path) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    checkout.mkdir(mode=0o755)

    with module.open_canonical_checkout_root(checkout) as root:
        checkout.rename(tmp_path / "displaced-checkout")
        checkout.mkdir(mode=0o755)
        with pytest.raises(module.ProvenanceError, match="checkout root"):
            root.revalidate()


def test_deployment_asset_allowlist_is_checked_before_any_child_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    workstation = checkout / "deploy" / "workstation"
    workstation.mkdir(parents=True)
    checkout.chmod(0o755)
    (checkout / "deploy").chmod(0o755)
    workstation.chmod(0o755)
    unapproved = workstation / "unapproved.yaml"
    unapproved.write_text("unapproved\n", encoding="utf-8")
    unapproved.chmod(0o644)
    open_calls: list[object] = []

    with module.open_canonical_checkout_root(checkout) as root:
        real_open = module.os.open

        def record_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            open_calls.append(path)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(module.os, "open", record_open)
        with pytest.raises(module.ProvenanceError, match="deployment asset path"):
            module.safe_read_deployment_asset(
                root,
                "deploy/workstation/unapproved.yaml",
            )

    assert open_calls == []


def test_deployment_asset_reader_rejects_the_private_state_as_a_trust_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    relative_path = "deploy/workstation/compose.yaml"
    asset = state_path / relative_path
    asset.parent.mkdir(parents=True)
    (state_path / "deploy").chmod(0o755)
    asset.parent.chmod(0o755)
    asset.write_text("FROM-WRONG-TRUST-ROOT\n", encoding="utf-8")
    asset.chmod(0o644)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="checkout root") as caught:
            module.safe_read_deployment_asset(state, relative_path)

    assert "FROM-WRONG-TRUST-ROOT" not in str(caught.value)


@pytest.mark.parametrize(
    "operation",
    (
        "canonical-path",
        "build-lock",
        "publication-probe",
        "publish-manifest",
        "confirm-manifest",
    ),
)
def test_state_only_apis_reject_a_checkout_root_before_mutation(
    operation: str,
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    checkout.mkdir(mode=0o755)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    result: object | None = None

    with module.open_canonical_checkout_root(checkout) as root:
        try:
            with pytest.raises(
                module.ProvenanceError,
                match="canonical state directory",
            ):
                if operation == "canonical-path":
                    result = module.canonical_manifest_path(root, TARGET_SHA)
                elif operation == "build-lock":
                    result = module.BuildLock.acquire(root, TARGET_SHA)
                elif operation == "publication-probe":
                    result = module.require_manifest_publication_support(root)
                elif operation == "publish-manifest":
                    result = module.publish_manifest_no_clobber(root, manifest)
                else:
                    result = module.confirm_existing_manifest_durable(root, manifest)
        finally:
            if isinstance(result, module.BuildLock):
                result.close()
            if isinstance(result, module.PublishedManifest):
                result.safe_read.close()

    assert list(checkout.iterdir()) == []


@pytest.mark.parametrize("unsafe_asset", ("wrong-mode", "oversized", "invalid-utf8"))
def test_deployment_asset_reader_enforces_its_fixed_file_policy(
    unsafe_asset: str,
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    workstation = checkout / "deploy" / "workstation"
    workstation.mkdir(parents=True)
    checkout.chmod(0o755)
    (checkout / "deploy").chmod(0o755)
    workstation.chmod(0o755)
    relative_path = "deploy/workstation/compose.yaml"
    asset = checkout / relative_path
    if unsafe_asset == "oversized":
        asset.write_bytes(b"x" * (1024 * 1024 + 1))
        asset.chmod(0o644)
    elif unsafe_asset == "invalid-utf8":
        asset.write_bytes(b"\xff")
        asset.chmod(0o644)
    else:
        asset.write_text("services: {}\n", encoding="utf-8")
        asset.chmod(0o600)

    with module.open_canonical_checkout_root(checkout) as root:
        with pytest.raises(module.ProvenanceError, match="file"):
            module.safe_read_deployment_asset(root, relative_path)


@pytest.mark.parametrize("replacement", ("parent-directory", "final-file"))
def test_deployment_asset_safe_read_rejects_checkout_path_replacement(
    replacement: str,
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    checkout = tmp_path / "checkout"
    workstation = checkout / "deploy" / "workstation"
    workstation.mkdir(parents=True)
    checkout.chmod(0o755)
    (checkout / "deploy").chmod(0o755)
    workstation.chmod(0o755)
    relative_path = "deploy/workstation/compose.yaml"
    asset = checkout / relative_path
    asset.write_text("services: {}\n", encoding="utf-8")
    asset.chmod(0o644)

    with module.open_canonical_checkout_root(checkout) as root:
        with module.safe_read_deployment_asset(root, relative_path) as opened:
            if replacement == "parent-directory":
                workstation.rename(checkout / "deploy" / "displaced-workstation")
                workstation.mkdir(mode=0o755)
                replacement_asset = workstation / "compose.yaml"
            else:
                asset.rename(workstation / "displaced-compose.yaml")
                replacement_asset = asset
            replacement_asset.write_text("replacement\n", encoding="utf-8")
            replacement_asset.chmod(0o644)

            with pytest.raises(module.ProvenanceError, match="file"):
                opened.revalidate()


def test_manifest_path_is_one_canonical_direct_state_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    expected = state_path / f"build-{TARGET_SHA}.json"

    with module.open_canonical_state_directory() as state:
        assert module.canonical_manifest_path(state, TARGET_SHA) == expected
        assert (
            module.require_canonical_manifest_path(state, expected, TARGET_SHA)
            == expected.name
        )
        assert module.source_sha_from_manifest_path(state, expected) == TARGET_SHA


@pytest.mark.parametrize(
    "supplied",
    (
        "alternate-parent",
        "short-sha",
        "uppercase-sha",
        "wrong-sha",
        "dotdot-parent",
        "extra-suffix",
    ),
)
def test_manifest_path_rejects_every_alternate_shape(
    supplied: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    expected = state_path / f"build-{TARGET_SHA}.json"
    if supplied == "alternate-parent":
        path = tmp_path / expected.name
    elif supplied == "short-sha":
        path = state_path / f"build-{TARGET_SHA[:12]}.json"
    elif supplied == "uppercase-sha":
        path = state_path / f"build-{TARGET_SHA.upper()}.json"
    elif supplied == "wrong-sha":
        path = state_path / f"build-{'f' * 40}.json"
    elif supplied == "dotdot-parent":
        path = state_path / ".." / "deploy-state" / expected.name
    else:
        path = state_path / f"{expected.name}.old"

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="manifest path"):
            module.require_canonical_manifest_path(state, path, TARGET_SHA)
        if supplied == "wrong-sha":
            assert module.source_sha_from_manifest_path(state, path) == "f" * 40
        else:
            with pytest.raises(module.ProvenanceError, match="manifest path"):
                module.source_sha_from_manifest_path(state, path)


def test_safe_read_direct_child_retains_and_revalidates_one_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / f"build-{TARGET_SHA}.json"
    content = b'{"safe":"fictional"}\n'
    child.write_bytes(content)
    child.chmod(0o600)

    with module.open_canonical_state_directory() as state:
        with module.safe_read_direct_child(
            state,
            child.name,
            expected_mode=0o600,
            maximum_size=256 * 1024,
        ) as opened:
            assert opened.data == content
            assert opened.descriptor >= 0
            assert opened.identity.size == len(content)
            assert opened.text == content.decode("utf-8")
            assert not os.get_inheritable(opened.descriptor)
            opened.revalidate()


@pytest.mark.parametrize(
    "unsafe_shape",
    (
        "wrong-mode",
        "hard-link",
        "directory",
        "symlink",
        "broken-symlink",
        "oversized",
        "invalid-utf8",
    ),
)
def test_safe_read_direct_child_rejects_unsafe_file_shapes(
    unsafe_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    name = "private.json"
    child = state_path / name
    maximum_size = 8
    if unsafe_shape == "wrong-mode":
        child.write_bytes(b"safe")
        child.chmod(0o644)
    elif unsafe_shape == "hard-link":
        child.write_bytes(b"safe")
        child.chmod(0o600)
        os.link(child, state_path / "second-link")
    elif unsafe_shape == "directory":
        child.mkdir(mode=0o600)
    elif unsafe_shape == "symlink":
        target = state_path / "target"
        target.write_bytes(b"safe")
        target.chmod(0o600)
        child.symlink_to(target.name)
    elif unsafe_shape == "broken-symlink":
        child.symlink_to("missing")
    elif unsafe_shape == "oversized":
        child.write_bytes(b"x" * (maximum_size + 1))
        child.chmod(0o600)
    else:
        child.write_bytes(b"\xff")
        child.chmod(0o600)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="file"):
            module.safe_read_direct_child(
                state,
                name,
                expected_mode=0o600,
                maximum_size=maximum_size,
            )


@pytest.mark.parametrize("name", ("", ".", "..", "nested/file", "/absolute"))
def test_safe_read_direct_child_rejects_non_child_names(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="file name"):
            module.safe_read_direct_child(
                state,
                name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_nested_child_binds_every_directory_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    nested = state_path / "assets" / "nested"
    nested.mkdir(mode=0o755, parents=True)
    (state_path / "assets").chmod(0o755)
    nested.chmod(0o755)
    asset = nested / "asset.txt"
    asset.write_text("verified asset\n", encoding="utf-8")
    asset.chmod(0o644)

    with module.open_canonical_state_directory() as state:
        with module.safe_read_nested_child(
            state,
            "assets/nested/asset.txt",
            expected_mode=0o644,
            maximum_size=1024 * 1024,
        ) as opened:
            assert opened.text == "verified asset\n"
            opened.revalidate()


@pytest.mark.parametrize(
    "relative_path",
    (
        "",
        "/absolute/file",
        "./asset",
        "../asset",
        "assets/../asset",
        "assets//asset",
        "assets/./asset",
    ),
)
def test_safe_read_nested_child_rejects_noncanonical_paths(
    relative_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="file path"):
            module.safe_read_nested_child(
                state,
                relative_path,
                expected_mode=0o644,
                maximum_size=1024 * 1024,
            )


@pytest.mark.parametrize("unsafe_directory", ("symlink", "group-writable"))
def test_safe_read_nested_child_rejects_unsafe_directories(
    unsafe_directory: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real = state_path / "real"
    real.mkdir(mode=0o755)
    asset = real / "asset.txt"
    asset.write_text("verified asset\n", encoding="utf-8")
    asset.chmod(0o644)
    nested = state_path / "nested"
    if unsafe_directory == "symlink":
        nested.symlink_to(real.name, target_is_directory=True)
    else:
        nested.mkdir(mode=0o770)
        nested.chmod(0o770)
        local_asset = nested / "asset.txt"
        local_asset.write_text("verified asset\n", encoding="utf-8")
        local_asset.chmod(0o644)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="file"):
            module.safe_read_nested_child(
                state,
                "nested/asset.txt",
                expected_mode=0o644,
                maximum_size=1024 * 1024,
            )


def test_safe_read_rejects_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    account_home, state_path = create_canonical_state_tree(tmp_path)
    fifo = state_path / "manifest.json"
    os.mkfifo(fifo, mode=0o600)
    fifo.chmod(0o600)
    probe = """
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

module_path = Path(sys.argv[1])
account_home = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("fifo_safe_read_probe", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=str(account_home))
try:
    with module.open_canonical_state_directory() as state:
        module.safe_read_direct_child(
            state,
            "manifest.json",
            expected_mode=0o600,
            maximum_size=256 * 1024,
        )
except module.ProvenanceError as error:
    assert "FIFO-CANARY" not in str(error)
    raise SystemExit(0)
raise SystemExit(3)
"""

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(PROVENANCE_MODULE),
            str(account_home),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_safe_read_rejects_socket_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    monkeypatch.chdir(state_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(child.name)
        child.chmod(0o600)
        with module.open_canonical_state_directory() as state:
            with pytest.raises(module.ProvenanceError, match="file"):
                module.safe_read_direct_child(
                    state,
                    child.name,
                    expected_mode=0o600,
                    maximum_size=256 * 1024,
                )


def test_safe_read_rejects_file_swapped_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"original")
    child.chmod(0o600)
    displaced = state_path / "displaced.json"
    real_open = module.os.open

    with module.open_canonical_state_directory() as state:
        swapped = False

        def swap_before_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if path == child.name and dir_fd == state.descriptor and not swapped:
                swapped = True
                child.rename(displaced)
                child.write_bytes(b"replaced")
                child.chmod(0o600)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(module.os, "open", swap_before_open)
        with pytest.raises(module.ProvenanceError, match="identity"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_file_swapped_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"original-content")
    child.chmod(0o600)
    displaced = state_path / "displaced.json"
    real_read = module.os.read
    swapped = False

    def swap_after_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        chunk = real_read(descriptor, size)
        if not swapped:
            swapped = True
            child.rename(displaced)
            child.write_bytes(b"replacement-data")
            child.chmod(0o600)
        return chunk

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", swap_after_read)
        with pytest.raises(module.ProvenanceError, match="identity"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_parent_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"original-content")
    child.chmod(0o600)
    displaced_state = state_path.with_name("deploy-state-displaced")
    real_read = module.os.read
    swapped = False

    def replace_parent_after_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        chunk = real_read(descriptor, size)
        if not swapped:
            swapped = True
            state_path.rename(displaced_state)
            state_path.mkdir(mode=0o700)
        return chunk

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", replace_parent_after_read)
        with pytest.raises(module.ProvenanceError, match="state directory"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_short_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"long-enough-content")
    child.chmod(0o600)
    real_read = module.os.read
    calls = 0

    def stop_after_partial_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_read(descriptor, max(1, size // 2))
        return b""

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", stop_after_partial_read)
        with pytest.raises(module.ProvenanceError, match="changed while reading"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_growth_beyond_initial_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"initial")
    child.chmod(0o600)
    real_read = module.os.read
    appended = False

    def append_after_read(descriptor: int, size: int) -> bytes:
        nonlocal appended
        chunk = real_read(descriptor, size)
        if not appended:
            appended = True
            with child.open("ab") as stream:
                stream.write(b"SECRET-GROWTH-CANARY")
        return chunk

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", append_after_read)
        with pytest.raises(
            module.ProvenanceError, match="changed while reading"
        ) as caught:
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )

    assert "SECRET-GROWTH-CANARY" not in str(caught.value)


@pytest.mark.parametrize("mutation", ("mode", "mtime", "size"))
def test_safe_read_rejects_in_place_metadata_change(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"metadata-change-target")
    child.chmod(0o600)
    real_read = module.os.read
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        requested = size if mutation != "size" else max(1, size // 2)
        chunk = real_read(descriptor, requested)
        if not mutated:
            mutated = True
            if mutation == "mode":
                child.chmod(0o640)
            elif mutation == "mtime":
                metadata = child.stat()
                os.utime(
                    child,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
                )
            else:
                child.write_bytes(chunk)
                child.chmod(0o600)
        return chunk

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", mutate_after_read)
        with pytest.raises(module.ProvenanceError, match="file"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_errors_do_not_leak_oserror_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"safe")
    child.chmod(0o600)
    real_open = module.os.open

    with module.open_canonical_state_directory() as state:

        def fail_target_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == child.name and dir_fd == state.descriptor:
                raise OSError("SECRET-OSERROR-CANARY")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(module.os, "open", fail_target_open)
        with pytest.raises(module.ProvenanceError) as caught:
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )

    assert "SECRET-OSERROR-CANARY" not in str(caught.value)


def test_safe_read_nested_child_rechecks_parent_chain_after_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    nested = state_path / "assets" / "nested"
    nested.mkdir(mode=0o755, parents=True)
    (state_path / "assets").chmod(0o755)
    nested.chmod(0o755)
    asset = nested / "asset.txt"
    asset.write_text("verified asset\n", encoding="utf-8")
    asset.chmod(0o644)

    with module.open_canonical_state_directory() as state:
        with module.safe_read_nested_child(
            state,
            "assets/nested/asset.txt",
            expected_mode=0o644,
            maximum_size=1024 * 1024,
        ) as opened:
            real_stat = module.os.stat
            swapped = False

            def replace_parent_after_file_stat(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                nonlocal swapped
                result = real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )
                if path == "asset.txt" and not swapped:
                    swapped = True
                    displaced = state_path / "assets-displaced"
                    (state_path / "assets").rename(displaced)
                    attacker = state_path / "assets" / "nested"
                    attacker.mkdir(mode=0o755, parents=True)
                    (state_path / "assets").chmod(0o755)
                    attacker.chmod(0o755)
                    replacement = attacker / "asset.txt"
                    replacement.write_text("attacker asset\n", encoding="utf-8")
                    replacement.chmod(0o644)
                return result

            monkeypatch.setattr(module.os, "stat", replace_parent_after_file_stat)
            with pytest.raises(module.ProvenanceError, match="directory identity"):
                opened.revalidate()


def test_safe_read_nested_failure_closes_each_parent_descriptor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    nested = state_path / "assets" / "nested"
    nested.mkdir(mode=0o755, parents=True)
    (state_path / "assets").chmod(0o755)
    nested.chmod(0o755)
    asset = nested / "asset.txt"
    asset.write_text("verified asset\n", encoding="utf-8")
    asset.chmod(0o644)
    directory_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    real_open_component = module._open_directory_component
    real_close = module.os.close

    def record_open_component(**kwargs: object) -> object:
        component = real_open_component(**kwargs)
        directory_descriptors.append(component.descriptor)
        return component

    def fail_initial_revalidation(_safe_read: object) -> None:
        raise module.ProvenanceError("forced safe file validation failure")

    def record_close(descriptor: int) -> None:
        if descriptor in directory_descriptors:
            closed_descriptors.append(descriptor)
        real_close(descriptor)

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module, "_open_directory_component", record_open_component)
        monkeypatch.setattr(module.SafeRead, "revalidate", fail_initial_revalidation)
        monkeypatch.setattr(module.os, "close", record_close)
        with pytest.raises(module.ProvenanceError, match="forced"):
            module.safe_read_nested_child(
                state,
                "assets/nested/asset.txt",
                expected_mode=0o644,
                maximum_size=1024 * 1024,
            )
        assert directory_descriptors
        assert closed_descriptors == list(reversed(directory_descriptors))


@pytest.mark.parametrize("home_shape", ("relative", "dotdot"))
def test_state_directory_rejects_noncanonical_passwd_home(
    home_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    raw_home = (
        "relative-account-home"
        if home_shape == "relative"
        else str(account_home.parent / "unused" / ".." / account_home.name)
    )
    monkeypatch.setattr(
        module.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=raw_home),
    )

    with pytest.raises(module.ProvenanceError, match="state directory"):
        module.open_canonical_state_directory()


def test_state_directory_rejects_wrong_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real_stat = module.os.stat

    def report_wrong_owner(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if Path(path) == account_home and kwargs.get("follow_symlinks") is False:
            values = list(result)
            values[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(module.os, "stat", report_wrong_owner)
    with pytest.raises(module.ProvenanceError, match="state directory"):
        module.open_canonical_state_directory()


def test_safe_read_rejects_wrong_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"safe")
    child.chmod(0o600)
    real_stat = module.os.stat

    with module.open_canonical_state_directory() as state:

        def report_wrong_owner(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            result = real_stat(path, *args, **kwargs)
            if path == child.name and kwargs.get("dir_fd") == state.descriptor:
                values = list(result)
                values[stat.ST_UID] = os.geteuid() + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(module.os, "stat", report_wrong_owner)
        with pytest.raises(module.ProvenanceError, match="file metadata"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_device_substitution_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"safe")
    child.chmod(0o600)
    real_open = module.os.open

    with module.open_canonical_state_directory() as state:

        def substitute_device(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == child.name and dir_fd == state.descriptor:
                return real_open("/dev/null", flags)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(module.os, "open", substitute_device)
        with pytest.raises(module.ProvenanceError, match="file metadata"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_ctime_only_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"ctime-change-target")
    child.chmod(0o600)
    real_read = module.os.read
    mutated = False

    def change_ctime_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if not mutated:
            mutated = True
            before = child.stat()
            child.chmod(0o640)
            child.chmod(0o600)
            after = child.stat()
            assert after.st_mtime_ns == before.st_mtime_ns
            assert after.st_ctime_ns != before.st_ctime_ns
        return chunk

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", change_ctime_after_read)
        with pytest.raises(module.ProvenanceError, match="file identity"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_size_drift_after_eof_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"size-change-target")
    child.chmod(0o600)
    real_read = module.os.read
    calls = 0

    def append_after_eof_probe(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        chunk = real_read(descriptor, size)
        if calls == 2:
            assert chunk == b""
            with child.open("ab") as stream:
                stream.write(b"x")
        return chunk

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.os, "read", append_after_eof_probe)
        with pytest.raises(module.ProvenanceError, match="file identity"):
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )


def test_safe_read_rejects_fifo_substituted_between_stat_and_open_without_blocking(
    tmp_path: Path,
) -> None:
    account_home, state_path = create_canonical_state_tree(tmp_path)
    child = state_path / "manifest.json"
    child.write_bytes(b"safe")
    child.chmod(0o600)
    probe = """
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import sys

module_path = Path(sys.argv[1])
account_home = Path(sys.argv[2])
child = account_home / "apps" / "commerce-ops-desk" / "deploy-state" / "manifest.json"
spec = importlib.util.spec_from_file_location("fifo_swap_safe_read_probe", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=str(account_home))
real_open = module.os.open
swapped = False
try:
    with module.open_canonical_state_directory() as state:
        def swap_fifo(path, flags, mode=0o777, *, dir_fd=None):
            global swapped
            if path == child.name and dir_fd == state.descriptor and not swapped:
                swapped = True
                child.unlink()
                os.mkfifo(child, mode=0o600)
                child.chmod(0o600)
            return real_open(path, flags, mode, dir_fd=dir_fd)
        module.os.open = swap_fifo
        module.safe_read_direct_child(
            state,
            child.name,
            expected_mode=0o600,
            maximum_size=256 * 1024,
        )
except module.ProvenanceError:
    raise SystemExit(0)
raise SystemExit(3)
"""

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(PROVENANCE_MODULE),
            str(account_home),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "unsafe_shape",
    ("wrong-mode", "hard-link", "symlink", "fifo", "oversized", "invalid-utf8"),
)
def test_safe_read_nested_child_rejects_unsafe_final_file(
    unsafe_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    nested = state_path / "assets"
    nested.mkdir(mode=0o755)
    nested.chmod(0o755)
    child = nested / "asset.txt"
    maximum_size = 8
    if unsafe_shape == "wrong-mode":
        child.write_bytes(b"safe")
        child.chmod(0o600)
    elif unsafe_shape == "hard-link":
        child.write_bytes(b"safe")
        child.chmod(0o644)
        os.link(child, nested / "second-link")
    elif unsafe_shape == "symlink":
        target = nested / "target"
        target.write_bytes(b"safe")
        target.chmod(0o644)
        child.symlink_to(target.name)
    elif unsafe_shape == "fifo":
        os.mkfifo(child, mode=0o644)
        child.chmod(0o644)
    elif unsafe_shape == "oversized":
        child.write_bytes(b"x" * (maximum_size + 1))
        child.chmod(0o644)
    else:
        child.write_bytes(b"\xff")
        child.chmod(0o644)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="file"):
            module.safe_read_nested_child(
                state,
                "assets/asset.txt",
                expected_mode=0o644,
                maximum_size=maximum_size,
            )


def test_safe_read_nested_component_fstat_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    nested = state_path / "assets"
    nested.mkdir(mode=0o755)
    nested.chmod(0o755)
    child = nested / "asset.txt"
    child.write_bytes(b"safe")
    child.chmod(0o644)
    real_open = module.os.open
    real_fstat = module.os.fstat
    nested_descriptor: int | None = None

    with module.open_canonical_state_directory() as state:

        def capture_nested_descriptor(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal nested_descriptor
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "assets" and dir_fd == state.descriptor:
                nested_descriptor = descriptor
            return descriptor

        def fail_nested_fstat(descriptor: int) -> os.stat_result:
            if descriptor == nested_descriptor:
                raise OSError("SECRET-NESTED-FSTAT-CANARY")
            return real_fstat(descriptor)

        monkeypatch.setattr(module.os, "open", capture_nested_descriptor)
        monkeypatch.setattr(module.os, "fstat", fail_nested_fstat)
        with pytest.raises(module.ProvenanceError) as caught:
            module.safe_read_nested_child(
                state,
                "assets/asset.txt",
                expected_mode=0o644,
                maximum_size=1024,
            )

    assert "SECRET-NESTED-FSTAT-CANARY" not in str(caught.value)


def test_build_lock_creates_private_full_sha_lock_and_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    expected = state_path / f".build-{TARGET_SHA}.lock"

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA) as lock:
            metadata = expected.lstat()
            assert lock.name == expected.name
            assert lock.source_sha == TARGET_SHA
            assert lock.descriptor >= 0
            assert not os.get_inheritable(lock.descriptor)
            assert metadata.st_uid == os.geteuid()
            assert metadata.st_nlink == 1
            assert metadata.st_size == 0
            assert metadata.st_mode & 0o777 == 0o600
            lock.revalidate()

    assert expected.is_file()
    assert expected.stat().st_size == 0


def test_build_lock_reopens_the_same_safe_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    expected = state_path / f".build-{TARGET_SHA}.lock"
    expected.touch(mode=0o600)
    expected.chmod(0o600)
    original_inode = expected.stat().st_ino

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA) as first:
            assert first.identity.inode == original_inode
        with module.BuildLock.acquire(state, TARGET_SHA) as second:
            assert second.identity.inode == original_inode

    assert expected.stat().st_ino == original_inode


@pytest.mark.parametrize(
    "invalid_sha",
    (
        "",
        TARGET_SHA[:-1],
        TARGET_SHA.upper(),
        "g" * 40,
        f"../{TARGET_SHA}",
        True,
        None,
    ),
)
def test_build_lock_rejects_invalid_source_sha_without_creating_a_file(
    invalid_sha: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="source SHA"):
            module.BuildLock.acquire(state, invalid_sha)

    assert list(state_path.glob(".build-*.lock")) == []


@pytest.mark.parametrize(
    "unsafe_shape",
    (
        "wrong-mode",
        "hard-link",
        "nonempty",
        "directory",
        "symlink",
        "broken-symlink",
        "socket",
    ),
)
def test_build_lock_rejects_unsafe_existing_objects_without_modifying_them(
    unsafe_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    lock_path = state_path / f".build-{TARGET_SHA}.lock"
    listener: socket.socket | None = None
    if unsafe_shape == "wrong-mode":
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o644)
    elif unsafe_shape == "hard-link":
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
        os.link(lock_path, state_path / "second-link")
    elif unsafe_shape == "nonempty":
        lock_path.write_bytes(b"do-not-truncate")
        lock_path.chmod(0o600)
    elif unsafe_shape == "directory":
        lock_path.mkdir(mode=0o700)
    elif unsafe_shape == "symlink":
        target = state_path / "target"
        target.touch(mode=0o600)
        target.chmod(0o600)
        lock_path.symlink_to(target.name)
    elif unsafe_shape == "broken-symlink":
        lock_path.symlink_to("missing")
    else:
        monkeypatch.chdir(state_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(lock_path.name)
        lock_path.chmod(0o600)
    original = lock_path.lstat()
    original_content = (
        lock_path.read_bytes() if unsafe_shape in {"wrong-mode", "nonempty"} else None
    )
    try:
        with module.open_canonical_state_directory() as state:
            with pytest.raises(module.ProvenanceError, match="lock|file"):
                module.BuildLock.acquire(state, TARGET_SHA)
    finally:
        if listener is not None:
            listener.close()

    current = lock_path.lstat()
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    if original_content is not None:
        assert lock_path.read_bytes() == original_content


def test_build_lock_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    account_home, state_path = create_canonical_state_tree(tmp_path)
    lock_path = state_path / f".build-{TARGET_SHA}.lock"
    os.mkfifo(lock_path, mode=0o600)
    lock_path.chmod(0o600)
    probe = """
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

module_path = Path(sys.argv[1])
account_home = Path(sys.argv[2])
source_sha = sys.argv[3]
spec = importlib.util.spec_from_file_location("build_lock_fifo_probe", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=str(account_home))
try:
    with module.open_canonical_state_directory() as state:
        module.BuildLock.acquire(state, source_sha)
except module.ProvenanceError as error:
    assert "FIFO-LOCK-CANARY" not in str(error)
    raise SystemExit(0)
raise SystemExit(3)
"""

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(PROVENANCE_MODULE),
            str(account_home),
            TARGET_SHA,
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert lock_path.is_fifo()


def test_build_lock_rejects_wrong_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    name = f".build-{TARGET_SHA}.lock"
    lock_path = state_path / name
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    real_open = module.os.open
    real_fstat = module.os.fstat
    lock_descriptor: int | None = None

    with module.open_canonical_state_directory() as state:

        def capture_lock_descriptor(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal lock_descriptor
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == name and dir_fd == state.descriptor:
                lock_descriptor = descriptor
            return descriptor

        def report_wrong_owner(descriptor: int) -> os.stat_result:
            result = real_fstat(descriptor)
            if descriptor == lock_descriptor:
                values = list(result)
                values[stat.ST_UID] = os.geteuid() + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(module.os, "open", capture_lock_descriptor)
        monkeypatch.setattr(module.os, "fstat", report_wrong_owner)
        with pytest.raises(module.ProvenanceError, match="file metadata"):
            module.BuildLock.acquire(state, TARGET_SHA)

    assert lock_path.exists()
    assert lock_path.stat().st_size == 0
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_second_build_lock_for_same_sha_fails_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    probe = """
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

module_path = Path(sys.argv[1])
account_home = Path(sys.argv[2])
source_sha = sys.argv[3]
spec = importlib.util.spec_from_file_location("build_lock_contention_probe", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=str(account_home))
try:
    with module.open_canonical_state_directory() as state:
        module.BuildLock.acquire(state, source_sha)
except module.ProvenanceError as error:
    if str(error) == "target operation already in progress":
        raise SystemExit(0)
    raise
raise SystemExit(3)
"""

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    probe,
                    str(PROVENANCE_MODULE),
                    str(account_home),
                    TARGET_SHA,
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )

    assert result.returncode == 0, result.stderr


def test_different_target_shas_can_be_locked_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA) as target_lock:
            with module.BuildLock.acquire(state, DONOR_SHA) as donor_lock:
                target_lock.revalidate()
                donor_lock.revalidate()

    assert (state_path / f".build-{TARGET_SHA}.lock").exists()
    assert (state_path / f".build-{DONOR_SHA}.lock").exists()


def test_build_lock_uses_exclusive_nonblocking_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real_flock = module.fcntl.flock
    operations: list[int] = []

    def record_flock(descriptor: int, operation: int) -> object:
        operations.append(operation)
        return real_flock(descriptor, operation)

    with module.open_canonical_state_directory() as state:
        monkeypatch.setattr(module.fcntl, "flock", record_flock)
        with module.BuildLock.acquire(state, TARGET_SHA):
            pass

    assert operations == [module.fcntl.LOCK_EX | module.fcntl.LOCK_NB]


@pytest.mark.parametrize(
    "interrupted_operation",
    ("create-open", "existing-open", "flock"),
)
def test_build_lock_retries_interrupted_acquisition_operations(
    interrupted_operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    name = f".build-{TARGET_SHA}.lock"
    lock_path = state_path / name
    if interrupted_operation == "existing-open":
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
    real_open = module.os.open
    real_flock = module.fcntl.flock
    interruptions = 0

    with module.open_canonical_state_directory() as state:

        def interrupt_open_once(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal interruptions
            is_create = bool(flags & os.O_EXCL)
            matches = (
                path == name
                and dir_fd == state.descriptor
                and (
                    (interrupted_operation == "create-open" and is_create)
                    or (interrupted_operation == "existing-open" and not is_create)
                )
            )
            if matches and interruptions == 0:
                interruptions += 1
                raise InterruptedError
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def interrupt_flock_once(descriptor: int, operation: int) -> object:
            nonlocal interruptions
            if interrupted_operation == "flock" and interruptions == 0:
                interruptions += 1
                raise InterruptedError
            return real_flock(descriptor, operation)

        monkeypatch.setattr(module.os, "open", interrupt_open_once)
        monkeypatch.setattr(module.fcntl, "flock", interrupt_flock_once)
        with module.BuildLock.acquire(state, TARGET_SHA) as lock:
            lock.revalidate()

    assert interruptions == 1


def test_build_lock_rejects_inode_swapped_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    name = f".build-{TARGET_SHA}.lock"
    lock_path = state_path / name
    displaced = state_path / "displaced.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    real_open = module.os.open
    swapped = False

    with module.open_canonical_state_directory() as state:

        def swap_before_existing_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if (
                path == name
                and dir_fd == state.descriptor
                and not flags & os.O_EXCL
                and not swapped
            ):
                swapped = True
                lock_path.rename(displaced)
                lock_path.touch(mode=0o600)
                lock_path.chmod(0o600)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(module.os, "open", swap_before_existing_open)
        with pytest.raises(module.ProvenanceError, match="identity"):
            module.BuildLock.acquire(state, TARGET_SHA)

    assert lock_path.exists()
    assert displaced.exists()


@pytest.mark.parametrize("replacement", ("inode", "parent"))
def test_build_lock_revalidates_after_flock(
    replacement: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    name = f".build-{TARGET_SHA}.lock"
    lock_path = state_path / name
    real_flock = module.fcntl.flock
    replaced = False

    with module.open_canonical_state_directory() as state:

        def replace_after_flock(descriptor: int, operation: int) -> object:
            nonlocal replaced
            result = real_flock(descriptor, operation)
            if not replaced:
                replaced = True
                if replacement == "inode":
                    lock_path.rename(state_path / "displaced.lock")
                    lock_path.touch(mode=0o600)
                    lock_path.chmod(0o600)
                else:
                    state_path.rename(state_path.with_name("deploy-state-displaced"))
                    state_path.mkdir(mode=0o700)
            return result

        monkeypatch.setattr(module.fcntl, "flock", replace_after_flock)
        with pytest.raises(module.ProvenanceError, match="identity|state directory"):
            module.BuildLock.acquire(state, TARGET_SHA)


@pytest.mark.parametrize("replacement", ("inode", "parent"))
def test_build_lock_revalidates_after_successful_body(
    replacement: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    lock_path = state_path / f".build-{TARGET_SHA}.lock"

    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="identity|state directory"):
            with module.BuildLock.acquire(state, TARGET_SHA):
                if replacement == "inode":
                    lock_path.rename(state_path / "displaced.lock")
                    lock_path.touch(mode=0o600)
                    lock_path.chmod(0o600)
                else:
                    state_path.rename(state_path.with_name("deploy-state-displaced"))
                    state_path.mkdir(mode=0o700)


def test_build_lock_preserves_body_exception_when_exit_revalidation_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    lock_path = state_path / f".build-{TARGET_SHA}.lock"

    with module.open_canonical_state_directory() as state:
        with pytest.raises(RuntimeError, match="body failure"):
            with module.BuildLock.acquire(state, TARGET_SHA):
                lock_path.rename(state_path / "displaced.lock")
                lock_path.touch(mode=0o600)
                lock_path.chmod(0o600)
                raise RuntimeError("body failure")

    assert lock_path.exists()
    assert (state_path / "displaced.lock").exists()


def test_rename_noreplace_uses_kernel_no_clobber_semantics() -> None:
    module = load_provenance_module()
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir():
        pytest.skip("/dev/shm is required for renameat2 coverage")
    root = Path(tempfile.mkdtemp(prefix="commerce-renameat2-", dir=shared_memory))
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        (root / "source").write_bytes(b"source")
        module._rename_noreplace(descriptor, "source", descriptor, "destination")
        assert not (root / "source").exists()
        assert (root / "destination").read_bytes() == b"source"

        (root / "second-source").write_bytes(b"second")
        with pytest.raises(OSError) as caught:
            module._rename_noreplace(
                descriptor,
                "second-source",
                descriptor,
                "destination",
            )
        assert caught.value.errno == errno.EEXIST
        assert not isinstance(caught.value, module._RenameNoreplaceError)
        assert (root / "second-source").read_bytes() == b"second"
        assert (root / "destination").read_bytes() == b"source"
    finally:
        os.close(descriptor)
        shutil.rmtree(root)


def test_publish_manifest_treats_verified_kernel_eexist_as_precommit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir():
        pytest.skip("/dev/shm is required for renameat2 coverage")
    root = Path(tempfile.mkdtemp(prefix="commerce-publish-eexist-", dir=shared_memory))
    try:
        account_home, state_path = create_canonical_state_tree(root)
        set_passwd_home(module, monkeypatch, account_home)
        manifest = module.parse_build_manifest_bytes(
            canonical_test_bytes(source_manifest_payload())
        )
        destination = state_path / f"build-{TARGET_SHA}.json"
        destination.write_bytes(b"existing-evidence")
        destination.chmod(0o600)

        with module.open_canonical_state_directory() as state:
            with module.BuildLock.acquire(state, TARGET_SHA):
                with pytest.raises(module.ManifestPublishError) as caught:
                    module.publish_manifest_no_clobber(state, manifest)

        assert caught.value.committed is False
        assert caught.value.cleanup_complete is True
        assert destination.read_bytes() == b"existing-evidence"
        assert not list(state_path.glob(f".{destination.name}.*.tmp"))
    finally:
        shutil.rmtree(root)


def test_publish_manifest_succeeds_with_native_no_clobber_and_retained_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir():
        pytest.skip("/dev/shm is required for renameat2 coverage")
    root = Path(tempfile.mkdtemp(prefix="commerce-publish-success-", dir=shared_memory))
    try:
        account_home, state_path = create_canonical_state_tree(root)
        set_passwd_home(module, monkeypatch, account_home)
        canonical_bytes = canonical_test_bytes(source_manifest_payload())
        manifest = module.parse_build_manifest_bytes(canonical_bytes)
        destination = state_path / f"build-{TARGET_SHA}.json"

        with module.open_canonical_state_directory() as state:
            with module.BuildLock.acquire(state, TARGET_SHA):
                published = module.publish_manifest_no_clobber(state, manifest)
                try:
                    assert published.canonical_bytes == canonical_bytes
                    assert published.safe_read.data == canonical_bytes
                    assert published.safe_read.identity == module._file_identity(
                        destination.stat()
                    )
                    published.safe_read.revalidate()
                finally:
                    published.safe_read.close()

        assert destination.read_bytes() == canonical_bytes
        assert not list(state_path.glob(f".{destination.name}.*.tmp"))
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize(
    ("error_number", "definitely_precommit"),
    (
        (errno.ENOSYS, True),
        (errno.EINVAL, True),
        (errno.EOPNOTSUPP, True),
        (errno.EEXIST, False),
        (errno.EINTR, False),
        (errno.EIO, False),
        (errno.ESTALE, False),
    ),
)
def test_rename_noreplace_only_classifies_definite_precommit_errors(
    error_number: int,
    definitely_precommit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()

    class FailingRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            module.ctypes.set_errno(error_number)
            return -1

    fake_libc = SimpleNamespace(renameat2=FailingRename())
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    with pytest.raises(OSError) as caught:
        module._rename_noreplace(3, "source", 3, "destination")

    assert caught.value.errno == error_number
    assert (
        isinstance(caught.value, module._RenameNoreplaceError) is definitely_precommit
    )


def test_publish_manifest_no_clobber_writes_canonical_private_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    real_rename = module.os.rename
    rename_calls: list[tuple[int, str, int, str]] = []

    def emulate_rename_noreplace(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        rename_calls.append(
            (source_dir_fd, source_name, destination_dir_fd, destination_name)
        )
        try:
            os.stat(
                destination_name,
                dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise module._RenameNoreplaceError(errno.EEXIST, "destination exists")
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    monkeypatch.setattr(module, "_rename_noreplace", emulate_rename_noreplace)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            published = module.publish_manifest_no_clobber(state, manifest)
            try:
                assert published.source_sha == TARGET_SHA
                assert published.canonical_bytes == canonical_bytes
                assert published.safe_read.data == canonical_bytes
                published.safe_read.revalidate()
                assert rename_calls == [
                    (
                        state.descriptor,
                        rename_calls[0][1],
                        state.descriptor,
                        f"build-{TARGET_SHA}.json",
                    )
                ]
            finally:
                published.safe_read.close()

    destination = state_path / f"build-{TARGET_SHA}.json"
    assert destination.read_bytes() == canonical_bytes
    metadata = destination.stat()
    assert metadata.st_mode & 0o777 == 0o600
    assert metadata.st_nlink == 1
    assert not list(state_path.glob(".*.tmp"))


def test_publish_manifest_handles_short_writes_and_hostile_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    real_open = module.os.open
    real_write = module.os.write
    real_rename = module.os.rename
    write_sizes: list[int] = []
    temporary_opens: list[tuple[int, int, int, bool]] = []

    def capture_temporary_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.endswith(".tmp"):
            temporary_opens.append(
                (
                    flags,
                    mode,
                    -1 if dir_fd is None else dir_fd,
                    os.get_inheritable(descriptor),
                )
            )
        return descriptor

    def short_write(descriptor: int, payload: bytes) -> int:
        portion = payload[: max(1, len(payload) // 3)]
        written = real_write(descriptor, portion)
        write_sizes.append(written)
        return written

    def emulate_rename(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            monkeypatch.setattr(module.os, "open", capture_temporary_open)
            monkeypatch.setattr(module.os, "write", short_write)
            monkeypatch.setattr(module, "_rename_noreplace", emulate_rename)
            previous_umask = os.umask(0o777)
            try:
                published = module.publish_manifest_no_clobber(state, manifest)
            finally:
                os.umask(previous_umask)
            published.safe_read.close()
            assert temporary_opens
            flags, mode, directory_descriptor, inheritable = temporary_opens[0]
            assert flags & os.O_EXCL
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_CLOEXEC
            assert mode == 0o600
            assert directory_descriptor == state.descriptor
            assert inheritable is False

    assert len(write_sizes) > 1
    destination = state_path / f"build-{TARGET_SHA}.json"
    assert destination.read_bytes() == canonical_bytes
    assert destination.stat().st_mode & 0o777 == 0o600


def test_publish_manifest_fsync_order_surrounds_commit_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    real_open = module.os.open
    real_fsync = module.os.fsync
    real_rename = module.os.rename
    temporary_descriptor: int | None = None
    events: list[str] = []

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):

            def capture_temporary_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal temporary_descriptor
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if isinstance(path, str) and path.endswith(".tmp"):
                    temporary_descriptor = descriptor
                return descriptor

            def record_fsync(descriptor: int) -> None:
                if descriptor == temporary_descriptor:
                    events.append("file-fsync")
                elif descriptor == state.descriptor:
                    events.append("state-fsync")
                real_fsync(descriptor)

            def record_rename(
                source_dir_fd: int,
                source_name: str,
                destination_dir_fd: int,
                destination_name: str,
            ) -> None:
                events.append("rename")
                real_rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                )

            monkeypatch.setattr(module.os, "open", capture_temporary_open)
            monkeypatch.setattr(module.os, "fsync", record_fsync)
            monkeypatch.setattr(module, "_rename_noreplace", record_rename)
            published = module.publish_manifest_no_clobber(state, manifest)
            published.safe_read.close()

    assert events == ["file-fsync", "rename", "file-fsync", "state-fsync"]


@pytest.mark.parametrize("destination_shape", ("regular", "broken-symlink"))
def test_publish_manifest_never_overwrites_existing_destination(
    destination_shape: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    destination = state_path / f"build-{TARGET_SHA}.json"
    if destination_shape == "regular":
        destination.write_bytes(b"existing-evidence")
        destination.chmod(0o600)
    else:
        destination.symlink_to("missing-evidence")
    original = destination.lstat()
    real_rename = module.os.rename

    def emulate_rename_noreplace(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        try:
            os.stat(
                destination_name,
                dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            real_rename(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )
            return
        raise OSError(errno.EEXIST, "destination exists")

    monkeypatch.setattr(module, "_rename_noreplace", emulate_rename_noreplace)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublishError) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is False
    current = destination.lstat()
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    if destination_shape == "regular":
        assert destination.read_bytes() == b"existing-evidence"
    else:
        assert destination.readlink() == Path("missing-evidence")
    assert not list(state_path.glob(f".{destination.name}.*.tmp"))


@pytest.mark.parametrize("error_number", (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP))
def test_publish_manifest_fails_closed_when_rename_noreplace_is_unsupported(
    error_number: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )

    def unsupported(*_args: object) -> None:
        raise module._RenameNoreplaceError(error_number, "SECRET-RENAME-CANARY")

    def forbidden_fallback(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("overwriting fallback was called")

    monkeypatch.setattr(module, "_rename_noreplace", unsupported)
    monkeypatch.setattr(module.os, "rename", forbidden_fallback)
    monkeypatch.setattr(module.os, "replace", forbidden_fallback)
    monkeypatch.setattr(module.os, "link", forbidden_fallback)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublishError) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is False
    assert "SECRET-RENAME-CANARY" not in str(caught.value)
    assert not (state_path / f"build-{TARGET_SHA}.json").exists()
    assert not list(state_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure_phase", ("write", "file-fsync", "rename"))
def test_publish_manifest_precommit_failure_cleans_verified_temporary(
    failure_phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    real_open = module.os.open
    real_write = module.os.write
    real_fsync = module.os.fsync
    real_rename = module.os.rename
    temporary_descriptor: int | None = None
    failed = False
    state_syncs = 0

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):

            def capture_temporary_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal temporary_descriptor
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if isinstance(path, str) and path.endswith(".tmp"):
                    temporary_descriptor = descriptor
                return descriptor

            def fail_write_once(descriptor: int, payload: bytes) -> int:
                nonlocal failed
                if failure_phase == "write" and not failed:
                    failed = True
                    raise OSError("SECRET-PRECOMMIT-CANARY")
                return real_write(descriptor, payload)

            def fail_fsync_once(descriptor: int) -> None:
                nonlocal failed, state_syncs
                if descriptor == state.descriptor:
                    state_syncs += 1
                if (
                    failure_phase == "file-fsync"
                    and descriptor == temporary_descriptor
                    and not failed
                ):
                    failed = True
                    raise OSError("SECRET-PRECOMMIT-CANARY")
                real_fsync(descriptor)

            def fail_or_commit_rename(
                source_dir_fd: int,
                source_name: str,
                destination_dir_fd: int,
                destination_name: str,
            ) -> None:
                nonlocal failed
                if failure_phase == "rename":
                    failed = True
                    raise module._RenameNoreplaceError(
                        errno.ENOSYS, "SECRET-PRECOMMIT-CANARY"
                    )
                real_rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                )

            monkeypatch.setattr(module.os, "open", capture_temporary_open)
            monkeypatch.setattr(module.os, "write", fail_write_once)
            monkeypatch.setattr(module.os, "fsync", fail_fsync_once)
            monkeypatch.setattr(module, "_rename_noreplace", fail_or_commit_rename)
            with pytest.raises(module.ManifestPublishError) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert failed
    assert caught.value.committed is False
    assert caught.value.cleanup_complete is True
    assert "SECRET-PRECOMMIT-CANARY" not in str(caught.value)
    assert state_syncs >= 1
    assert not (state_path / f"build-{TARGET_SHA}.json").exists()
    assert not list(state_path.glob(".*.tmp"))


def test_publish_manifest_precommit_cleanup_never_unlinks_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    real_open = module.os.open
    real_write = module.os.write
    real_rename = module.os.rename
    temporary_name: str | None = None
    replaced = False

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):

            def capture_temporary_name(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal temporary_name
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if isinstance(path, str) and path.endswith(".tmp"):
                    temporary_name = path
                return descriptor

            def replace_then_fail_write(descriptor: int, payload: bytes) -> int:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    assert temporary_name is not None
                    real_rename(
                        temporary_name,
                        "displaced-temp-evidence",
                        src_dir_fd=state.descriptor,
                        dst_dir_fd=state.descriptor,
                    )
                    replacement = state_path / temporary_name
                    replacement.write_bytes(b"replacement-must-survive")
                    replacement.chmod(0o600)
                    raise OSError("SECRET-REPLACEMENT-CANARY")
                return real_write(descriptor, payload)

            monkeypatch.setattr(module.os, "open", capture_temporary_name)
            monkeypatch.setattr(module.os, "write", replace_then_fail_write)
            with pytest.raises(module.ManifestPublishError) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert temporary_name is not None
    assert caught.value.committed is False
    assert caught.value.cleanup_complete is False
    replacement = state_path / temporary_name
    assert replacement.read_bytes() == b"replacement-must-survive"
    assert (state_path / "displaced-temp-evidence").exists()
    assert not (state_path / f"build-{TARGET_SHA}.json").exists()


@pytest.mark.parametrize("cleanup_failure", ("unlink", "directory-fsync"))
def test_publish_manifest_reports_incomplete_precommit_cleanup(
    cleanup_failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    real_open = module.os.open
    real_unlink = module.os.unlink
    real_fsync = module.os.fsync
    temporary_name: str | None = None

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):

            def capture_temporary_name(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal temporary_name
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if isinstance(path, str) and path.endswith(".tmp"):
                    temporary_name = path
                return descriptor

            def fail_write(_descriptor: int, _payload: bytes) -> int:
                raise OSError("SECRET-PUBLISH-FAILURE")

            def fail_selected_unlink(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
            ) -> None:
                if cleanup_failure == "unlink" and path == temporary_name:
                    raise OSError(errno.EIO, "SECRET-CLEANUP-FAILURE")
                real_unlink(path, dir_fd=dir_fd)

            def fail_selected_fsync(descriptor: int) -> None:
                if (
                    cleanup_failure == "directory-fsync"
                    and descriptor == state.descriptor
                ):
                    raise OSError(errno.EIO, "SECRET-CLEANUP-FAILURE")
                real_fsync(descriptor)

            monkeypatch.setattr(module.os, "open", capture_temporary_name)
            monkeypatch.setattr(module.os, "write", fail_write)
            monkeypatch.setattr(module.os, "unlink", fail_selected_unlink)
            monkeypatch.setattr(module.os, "fsync", fail_selected_fsync)
            with pytest.raises(module.ManifestPublishError) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert temporary_name is not None
    assert caught.value.committed is False
    assert caught.value.cleanup_complete is False
    assert "cleanup is incomplete" in str(caught.value)
    assert "SECRET-" not in str(caught.value)
    assert not (state_path / f"build-{TARGET_SHA}.json").exists()
    temporary_path = state_path / temporary_name
    assert temporary_path.exists() is (cleanup_failure == "unlink")


def test_publish_manifest_retries_interrupted_precommit_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    manifest = module.parse_build_manifest_bytes(
        canonical_test_bytes(source_manifest_payload())
    )
    real_unlink = module.os.unlink
    interrupted = False

    def fail_write(_descriptor: int, _payload: bytes) -> int:
        raise OSError("SECRET-PUBLISH-FAILURE")

    def interrupt_once(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal interrupted
        if not interrupted and isinstance(path, str) and path.endswith(".tmp"):
            interrupted = True
            raise InterruptedError(errno.EINTR, "SECRET-INTERRUPTED-UNLINK")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "write", fail_write)
    monkeypatch.setattr(module.os, "unlink", interrupt_once)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublishError) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert interrupted
    assert caught.value.cleanup_complete is True
    assert "SECRET-" not in str(caught.value)
    assert not list(state_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "failure_phase",
    ("post-file-fsync", "parent-fsync", "safe-read", "byte-confirmation"),
)
def test_publish_manifest_postcommit_failure_is_indeterminate_and_keeps_manifest(
    failure_phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    destination_name = f"build-{TARGET_SHA}.json"
    destination = state_path / destination_name
    real_open = module.os.open
    real_fsync = module.os.fsync
    real_rename = module.os.rename
    real_safe_read = module.safe_read_direct_child
    real_unlink = module.os.unlink
    temporary_descriptor: int | None = None
    committed = False
    file_syncs = 0
    unlink_calls: list[str | bytes] = []

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):

            def capture_temporary_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal temporary_descriptor
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if isinstance(path, str) and path.endswith(".tmp"):
                    temporary_descriptor = descriptor
                return descriptor

            def commit_rename(
                source_dir_fd: int,
                source_name: str,
                destination_dir_fd: int,
                requested_destination: str,
            ) -> None:
                nonlocal committed
                real_rename(
                    source_name,
                    requested_destination,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                )
                committed = True

            def fail_selected_fsync(descriptor: int) -> None:
                nonlocal file_syncs
                if descriptor == temporary_descriptor:
                    file_syncs += 1
                    if failure_phase == "post-file-fsync" and file_syncs == 2:
                        raise OSError("SECRET-POSTCOMMIT-CANARY")
                if (
                    failure_phase == "parent-fsync"
                    and committed
                    and descriptor == state.descriptor
                ):
                    raise OSError("SECRET-POSTCOMMIT-CANARY")
                real_fsync(descriptor)

            def fail_or_mutate_safe_read(
                parent: object,
                name: str,
                **kwargs: object,
            ) -> object:
                if failure_phase == "safe-read":
                    raise module.ProvenanceError("SECRET-POSTCOMMIT-CANARY")
                if failure_phase == "byte-confirmation":
                    with destination.open("ab") as stream:
                        stream.write(b"changed")
                return real_safe_read(parent, name, **kwargs)

            def record_unlink(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
            ) -> None:
                unlink_calls.append(os.fspath(path))
                real_unlink(path, dir_fd=dir_fd)

            monkeypatch.setattr(module.os, "open", capture_temporary_open)
            monkeypatch.setattr(module.os, "fsync", fail_selected_fsync)
            monkeypatch.setattr(module, "_rename_noreplace", commit_rename)
            monkeypatch.setattr(
                module, "safe_read_direct_child", fail_or_mutate_safe_read
            )
            monkeypatch.setattr(module.os, "unlink", record_unlink)
            with pytest.raises(module.ManifestPublicationIndeterminate) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert committed
    assert caught.value.committed is True
    assert "SECRET-POSTCOMMIT-CANARY" not in str(caught.value)
    assert destination.exists()
    assert destination_name not in unlink_calls
    if failure_phase != "byte-confirmation":
        assert destination.read_bytes() == canonical_bytes


def test_publish_manifest_interrupt_during_rename_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    real_rename = module.os.rename

    def commit_then_interrupt(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_rename_noreplace", commit_then_interrupt)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublicationIndeterminate) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is True
    destination = state_path / f"build-{TARGET_SHA}.json"
    assert destination.read_bytes() == canonical_bytes


def test_publish_manifest_replayed_eexist_after_commit_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    real_rename = module.os.rename

    def commit_then_report_eexist(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )
        raise OSError(errno.EEXIST, "SECRET-REPLAYED-EEXIST")

    monkeypatch.setattr(module, "_rename_noreplace", commit_then_report_eexist)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublicationIndeterminate) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is True
    assert "SECRET-REPLAYED-EEXIST" not in str(caught.value)
    destination = state_path / f"build-{TARGET_SHA}.json"
    assert destination.read_bytes() == canonical_bytes


def test_publish_manifest_rejects_staged_identity_drift_across_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    real_rename = module.os.rename

    def mutate_then_commit(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        source = state_path / source_name
        metadata = source.stat()
        os.utime(
            source,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000),
        )
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    monkeypatch.setattr(module, "_rename_noreplace", mutate_then_commit)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublicationIndeterminate) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is True
    destination = state_path / f"build-{TARGET_SHA}.json"
    assert destination.read_bytes() == canonical_bytes


def test_publish_manifest_rejects_identity_drift_before_final_safe_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    destination = state_path / f"build-{TARGET_SHA}.json"
    real_rename = module.os.rename
    real_safe_read = module.safe_read_direct_child

    def commit_rename(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    def drift_then_read(parent: object, name: str, **kwargs: object) -> object:
        destination.chmod(0o640)
        destination.chmod(0o600)
        return real_safe_read(parent, name, **kwargs)

    monkeypatch.setattr(module, "_rename_noreplace", commit_rename)
    monkeypatch.setattr(module, "safe_read_direct_child", drift_then_read)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublicationIndeterminate) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is True
    assert destination.read_bytes() == canonical_bytes


def test_confirm_existing_manifest_durable_accepts_only_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    destination = state_path / f"build-{TARGET_SHA}.json"
    destination.write_bytes(canonical_bytes)
    destination.chmod(0o600)

    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            confirmed = module.confirm_existing_manifest_durable(state, manifest)
            try:
                assert confirmed.canonical_bytes == canonical_bytes
                assert confirmed.safe_read.data == canonical_bytes
                confirmed.safe_read.revalidate()
            finally:
                confirmed.safe_read.close()

    alternate = json.dumps(source_manifest_payload(), sort_keys=True).encode("utf-8")
    destination.write_bytes(alternate)
    destination.chmod(0o600)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ProvenanceError, match="do not match"):
                module.confirm_existing_manifest_durable(state, manifest)

    assert destination.read_bytes() == alternate


def test_manifest_publication_support_probe_checks_both_no_clobber_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real_rename = module.os.rename
    calls: list[bool] = []

    def emulate_rename_noreplace(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        try:
            os.stat(
                destination_name,
                dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            calls.append(False)
            real_rename(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )
            return
        calls.append(True)
        raise OSError(errno.EEXIST, "destination exists")

    monkeypatch.setattr(module, "_rename_noreplace", emulate_rename_noreplace)
    with module.open_canonical_state_directory() as state:
        module.require_manifest_publication_support(state)
        state.revalidate()

    assert calls == [True, False]
    assert not list(state_path.glob(".rename-noreplace-probe-*.tmp"))


def test_manifest_publication_support_probe_succeeds_with_native_syscalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir():
        pytest.skip("/dev/shm is required for renameat2 coverage")
    root = Path(tempfile.mkdtemp(prefix="commerce-probe-success-", dir=shared_memory))
    try:
        account_home, state_path = create_canonical_state_tree(root)
        set_passwd_home(module, monkeypatch, account_home)

        with module.open_canonical_state_directory() as state:
            module.require_manifest_publication_support(state)
            state.revalidate()

        assert not list(state_path.glob(".rename-noreplace-probe-*.tmp"))
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("error_number", (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP))
def test_manifest_publication_support_probe_fails_closed_and_cleans(
    error_number: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)

    def unsupported(*_args: object) -> None:
        raise OSError(error_number, "SECRET-PROBE-CANARY")

    monkeypatch.setattr(module, "_rename_noreplace", unsupported)
    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="not supported") as caught:
            module.require_manifest_publication_support(state)

    assert "SECRET-PROBE-CANARY" not in str(caught.value)
    assert not list(state_path.glob(".rename-noreplace-probe-*.tmp"))


@pytest.mark.parametrize("failure_phase", ("file-fsync", "directory-fsync"))
def test_manifest_publication_support_probe_requires_durability_operations(
    failure_phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real_open = module.os.open
    real_fsync = module.os.fsync
    real_rename = module.os.rename
    probe_descriptors: set[int] = set()

    with module.open_canonical_state_directory() as state:

        def capture_probe_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if isinstance(path, str) and path.startswith(".rename-noreplace-probe-"):
                probe_descriptors.add(descriptor)
            return descriptor

        def fail_selected_fsync(descriptor: int) -> None:
            if failure_phase == "file-fsync" and descriptor in probe_descriptors:
                raise OSError(errno.EINVAL, "SECRET-PROBE-FSYNC-CANARY")
            if failure_phase == "directory-fsync" and descriptor == state.descriptor:
                raise OSError(errno.EINVAL, "SECRET-PROBE-FSYNC-CANARY")
            real_fsync(descriptor)

        def emulate_rename_noreplace(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            try:
                os.stat(
                    destination_name,
                    dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                real_rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                )
                return
            raise OSError(errno.EEXIST, "destination exists")

        monkeypatch.setattr(module.os, "open", capture_probe_open)
        monkeypatch.setattr(module.os, "fsync", fail_selected_fsync)
        monkeypatch.setattr(module, "_rename_noreplace", emulate_rename_noreplace)
        with pytest.raises(module.ProvenanceError, match="not supported") as caught:
            module.require_manifest_publication_support(state)

    assert "SECRET-PROBE-FSYNC-CANARY" not in str(caught.value)


@pytest.mark.parametrize("cleanup_failure", ("unlink", "directory-fsync"))
def test_manifest_publication_support_probe_rejects_final_cleanup_failure(
    cleanup_failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, _ = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real_rename = module.os.rename
    real_unlink = module.os.unlink
    real_fsync = module.os.fsync
    rename_calls = 0
    state_syncs = 0

    with module.open_canonical_state_directory() as state:

        def emulate_rename_noreplace(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal rename_calls
            rename_calls += 1
            try:
                os.stat(
                    destination_name,
                    dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                real_rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                )
                return
            raise OSError(errno.EEXIST, "destination exists")

        def fail_final_unlink(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *,
            dir_fd: int | None = None,
        ) -> None:
            if cleanup_failure == "unlink" and rename_calls == 2:
                raise OSError(errno.EIO, "SECRET-FINAL-CLEANUP-CANARY")
            real_unlink(path, dir_fd=dir_fd)

        def fail_final_directory_fsync(descriptor: int) -> None:
            nonlocal state_syncs
            if descriptor == state.descriptor:
                state_syncs += 1
                if cleanup_failure == "directory-fsync" and state_syncs == 3:
                    raise OSError(errno.EIO, "SECRET-FINAL-CLEANUP-CANARY")
            real_fsync(descriptor)

        monkeypatch.setattr(module, "_rename_noreplace", emulate_rename_noreplace)
        monkeypatch.setattr(module.os, "unlink", fail_final_unlink)
        monkeypatch.setattr(module.os, "fsync", fail_final_directory_fsync)
        with pytest.raises(module.ProvenanceError, match="not supported") as caught:
            module.require_manifest_publication_support(state)

    assert rename_calls == 2
    assert "SECRET-FINAL-CLEANUP-CANARY" not in str(caught.value)


def test_publish_manifest_oserror_after_committed_rename_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    canonical_bytes = canonical_test_bytes(source_manifest_payload())
    manifest = module.parse_build_manifest_bytes(canonical_bytes)
    real_rename = module.os.rename

    def commit_then_raise_oserror(
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )
        raise TimeoutError("SECRET-POST-RENAME-OSERROR")

    monkeypatch.setattr(module, "_rename_noreplace", commit_then_raise_oserror)
    with module.open_canonical_state_directory() as state:
        with module.BuildLock.acquire(state, TARGET_SHA):
            with pytest.raises(module.ManifestPublicationIndeterminate) as caught:
                module.publish_manifest_no_clobber(state, manifest)

    assert caught.value.committed is True
    assert "SECRET-POST-RENAME-OSERROR" not in str(caught.value)
    destination = state_path / f"build-{TARGET_SHA}.json"
    assert destination.read_bytes() == canonical_bytes


def test_manifest_publication_support_probe_cleans_after_fchmod_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    real_open = module.os.open
    opened_descriptors: list[int] = []

    def capture_probe_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".rename-noreplace-probe-"):
            opened_descriptors.append(descriptor)
        return descriptor

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("SECRET-PROBE-FCHMOD-CANARY")

    monkeypatch.setattr(module.os, "open", capture_probe_open)
    monkeypatch.setattr(module.os, "fchmod", fail_fchmod)
    with module.open_canonical_state_directory() as state:
        with pytest.raises(module.ProvenanceError, match="not supported") as caught:
            module.require_manifest_publication_support(state)

    assert "SECRET-PROBE-FCHMOD-CANARY" not in str(caught.value)
    assert opened_descriptors
    for descriptor in opened_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not list(state_path.glob(".rename-noreplace-probe-*.tmp"))


def test_safe_read_close_error_does_not_override_sanitized_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    account_home, state_path = create_canonical_state_tree(tmp_path)
    set_passwd_home(module, monkeypatch, account_home)
    child = state_path / "manifest.json"
    child.write_bytes(b"safe")
    child.chmod(0o644)
    real_open = module.os.open
    real_close = module.os.close
    child_descriptor: int | None = None

    with module.open_canonical_state_directory() as state:

        def capture_child_descriptor(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal child_descriptor
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == child.name and dir_fd == state.descriptor:
                child_descriptor = descriptor
            return descriptor

        def close_then_raise(descriptor: int) -> None:
            real_close(descriptor)
            if descriptor == child_descriptor:
                raise OSError("SECRET-CLOSE-CANARY")

        monkeypatch.setattr(module.os, "open", capture_child_descriptor)
        monkeypatch.setattr(module.os, "close", close_then_raise)
        with pytest.raises(module.ProvenanceError) as caught:
            module.safe_read_direct_child(
                state,
                child.name,
                expected_mode=0o600,
                maximum_size=256 * 1024,
            )

    assert "SECRET-CLOSE-CANARY" not in str(caught.value)
