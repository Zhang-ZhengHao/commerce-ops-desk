"""Contracts for shared verified build and deployment provenance helpers."""

# Nested contexts make descriptor and lock ownership explicit in these tests.
# ruff: noqa: SIM117

from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import FrozenInstanceError, replace
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
TEST_NOREPLY_EMAIL = "123456+provenance-test" + "@users." + "noreply.github.com"
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


def run_test_git(repository: Path, *arguments: str) -> bytes:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    result = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def create_provenance_git_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    run_test_git(repository, "init", "--initial-branch=main")
    run_test_git(repository, "config", "user.name", "Provenance Test")
    run_test_git(
        repository,
        "config",
        "user.email",
        TEST_NOREPLY_EMAIL,
    )
    (repository / "tracked.txt").write_text("approved bytes\n", encoding="utf-8")
    (repository / ".dockerignore").write_text(".git\n", encoding="utf-8")
    repository_files = {
        "Dockerfile": "FROM scratch\n",
        "backend/requirements.lock": "locked-backend==1.0\n",
        "frontend/package.json": "{}\n",
        "frontend/package-lock.json": '{"lockfileVersion":3}\n',
        "scripts/start-hosted.sh": "#!/bin/sh\nexec true\n",
        "deploy/workstation/compose.yaml": "services: {}\n",
        "deploy/workstation/commerce-ops-desk.Caddyfile.template": (
            "http://example.invalid { respond 200 }\n"
        ),
        "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template": (
            "http://example.invalid { respond 200 }\n"
        ),
    }
    for relative_path, content in repository_files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (repository / "scripts" / "start-hosted.sh").chmod(0o755)
    run_test_git(repository, "add", ".")
    run_test_git(repository, "commit", "--message", "approved")
    source_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    approved_ref = "refs/remotes/origin/main"
    run_test_git(repository, "update-ref", approved_ref, source_sha)
    return repository, source_sha, approved_ref


def build_test_git_archive(
    source_sha: str,
    members: tuple[tuple[str, bytes | None, int], ...],
) -> bytes:
    tar_members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    for name, content, mode in members:
        member = tarfile.TarInfo(name)
        member.mode = mode
        if content is None:
            member.type = tarfile.DIRTYPE
            member.size = 0
        else:
            member.type = tarfile.REGTYPE
            member.size = len(content)
        tar_members.append((member, content))
    return build_test_git_archive_from_infos(source_sha, tuple(tar_members))


def build_test_git_archive_from_infos(
    source_sha: str,
    members: tuple[tuple[tarfile.TarInfo, bytes | None], ...],
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w:",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": source_sha},
    ) as archive:
        for member, content in members:
            archive.addfile(
                member,
                None if content is None else io.BytesIO(content),
            )
    return output.getvalue()


def build_test_gnu_longname_archive(
    source_sha: str,
    *,
    name: str,
    content: bytes,
    mode: int,
) -> bytes:
    global_pax_blocks = build_test_git_archive(source_sha, ())[:1024]
    gnu_output = io.BytesIO()
    with tarfile.open(
        fileobj=gnu_output,
        mode="w:",
        format=tarfile.GNU_FORMAT,
    ) as archive:
        member = tarfile.TarInfo(name)
        member.mode = mode
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return global_pax_blocks + gnu_output.getvalue()


def truncate_tar_to_end_blocks(raw: bytes, end_blocks: int) -> bytes:
    offset = 0
    while raw[offset : offset + tarfile.BLOCKSIZE] != b"\0" * tarfile.BLOCKSIZE:
        header = raw[offset : offset + tarfile.BLOCKSIZE]
        size_field = header[124:136].rstrip(b"\0 ").lstrip(b" ") or b"0"
        size = int(size_field, 8)
        offset += tarfile.BLOCKSIZE + (
            ((size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
        )
    return raw[: offset + (end_blocks * tarfile.BLOCKSIZE)]


def tar_header_offsets(raw: bytes) -> list[int]:
    offsets: list[int] = []
    offset = 0
    zero_block = b"\0" * tarfile.BLOCKSIZE
    while raw[offset : offset + tarfile.BLOCKSIZE] != zero_block:
        offsets.append(offset)
        header = raw[offset : offset + tarfile.BLOCKSIZE]
        size_field = header[124:136].rstrip(b"\0 ").lstrip(b" ") or b"0"
        size = int(size_field, 8)
        offset += tarfile.BLOCKSIZE + (
            ((size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
        )
    return offsets


def rewrite_test_tar_checksum(raw: bytearray, offset: int) -> None:
    raw[offset + 148 : offset + 156] = b"        "
    checksum = sum(raw[offset : offset + tarfile.BLOCKSIZE])
    raw[offset + 148 : offset + 156] = f"{checksum:06o}\0 ".encode("ascii")


def make_policy_revision(
    module: ModuleType,
    source_sha: str,
    *,
    dockerfile_mode: str,
    dockerfile_blob_sha: str,
    dockerfile_sha256: str,
    component_suffix: bytes = b"",
) -> object:
    components = {
        name: module.CanonicalDigest(
            canonical_bytes=f"canonical {name}".encode() + component_suffix,
            sha256=module.length_framed_sha256(
                module.RUNTIME_COMPONENT_DOMAIN,
                f"canonical {name}".encode() + component_suffix,
            ),
        )
        for name in ("backend", "dockerignore", "frontend", "scripts")
    }
    combined = module._canonical_combined_runtime_inventory(
        {name: evidence.sha256 for name, evidence in components.items()}
    )
    return module.RevisionEvidence(
        source_sha=source_sha,
        deployment_assets=module.DeploymentAssets(schema=1, sha256={}),
        runtime_inventory=module.RuntimeInventory(
            components=components,
            combined=combined,
        ),
        dockerfile_mode=dockerfile_mode,
        dockerfile_blob_sha=dockerfile_blob_sha,
        dockerfile_sha256=dockerfile_sha256,
    )


def test_isolated_git_environment_replaces_every_ambient_trust_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    hostile = {
        "PATH": str(tmp_path / "attacker-bin"),
        "HOME": str(tmp_path / "attacker-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "attacker-xdg"),
        "GIT_DIR": str(tmp_path / "attacker.git"),
        "GIT_WORK_TREE": str(tmp_path / "attacker-tree"),
        "GIT_OBJECT_DIRECTORY": str(tmp_path / "attacker-objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path / "attacker-alternates"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.attributesFile",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "attacker-attributes"),
        "GIT_REPLACE_REF_BASE": "refs/attacker/replace/",
        "GIT_INDEX_FILE": str(tmp_path / "attacker-index"),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    environment = module._isolated_git_environment(tmp_path)

    assert environment == {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": environment["HOME"],
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "XDG_CONFIG_HOME": environment["XDG_CONFIG_HOME"],
    }
    for name in ("HOME", "XDG_CONFIG_HOME"):
        directory = Path(environment[name])
        assert directory.parent == tmp_path
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert environment[name] != hostile[name]
    for name, value in hostile.items():
        assert environment.get(name) != value


def test_git_bytes_runner_uses_fixed_binary_despite_hostile_path(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    attacker_bin = tmp_path / "attacker-bin"
    attacker_bin.mkdir()
    marker = tmp_path / "attacker-ran"
    fake_git = attacker_bin / "git"
    fake_git.write_text(
        f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 93\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    environment = module._isolated_git_environment(tmp_path)
    environment["PATH"] = f"{attacker_bin}:/usr/bin:/bin"

    output = module._run_git_bytes(
        ("version",),
        cwd=tmp_path,
        environment=environment,
    )

    assert output.startswith(b"git version ")
    assert not marker.exists()


def test_git_bytes_runner_rejects_oversize_output_without_leaking_it(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["/usr/bin/git", "init", "--bare", str(repository)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    canary = b"SECRET-GIT-OUTPUT-CANARY"
    object_id = (
        subprocess.run(
            ["/usr/bin/git", "hash-object", "-w", "--stdin"],
            cwd=repository,
            input=canary,
            check=True,
            capture_output=True,
        )
        .stdout.decode("ascii")
        .removesuffix("\n")
    )
    environment = module._isolated_git_environment(tmp_path)

    with pytest.raises(module.ProvenanceError) as captured:
        module._run_git_bytes(
            (f"--git-dir={repository}", "cat-file", "blob", object_id),
            cwd=tmp_path,
            environment=environment,
            maximum_stdout_bytes=len(canary) - 1,
        )

    assert canary.decode("ascii") not in str(captured.value)


def test_approved_git_revision_requires_exact_root_commit_and_remote_ref(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    repository, source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        module._assert_approved_git_revision(
            checkout,
            source_sha,
            approved_ref,
            temporary_directory=workspace,
        )


@pytest.mark.parametrize(
    ("source_sha", "approved_ref"),
    (
        ("a" * 39, "refs/remotes/origin/main"),
        ("A" * 40, "refs/remotes/origin/main"),
        ("a" * 40, "refs/heads/main"),
        ("a" * 40, "refs/remotes/origin/main\nrefs/heads/attack"),
    ),
)
def test_approved_git_revision_rejects_noncanonical_identity_inputs(
    tmp_path: Path,
    source_sha: str,
    approved_ref: str,
) -> None:
    module = load_provenance_module()
    repository, _actual_sha, _actual_ref = create_provenance_git_repository(tmp_path)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module._assert_approved_git_revision(
                checkout,
                source_sha,
                approved_ref,
                temporary_directory=workspace,
            )


def test_approved_git_revision_rejects_nested_checkout_and_ref_drift(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    repository, source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    (repository / "nested").mkdir()
    (repository / "tracked.txt").write_text("later bytes\n", encoding="utf-8")
    run_test_git(repository, "add", "tracked.txt")
    run_test_git(repository, "commit", "--message", "later")
    later_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module._assert_approved_git_revision(
                checkout,
                later_sha,
                approved_ref,
                temporary_directory=workspace,
            )
    with module.open_canonical_checkout_root(
        (repository / "nested").resolve()
    ) as nested_checkout:
        with pytest.raises(module.ProvenanceError):
            module._assert_approved_git_revision(
                nested_checkout,
                source_sha,
                approved_ref,
                temporary_directory=workspace,
            )


def test_isolated_object_view_reads_only_the_validated_object_database(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    source, source_sha, _approved_ref = create_provenance_git_repository(source_parent)
    shared = tmp_path / "shared"
    shared.mkdir()
    clone = tmp_path / "shared-clone"
    run_test_git(shared, "clone", "--shared", str(source), str(clone))
    assert (clone / ".git" / "objects" / "info" / "alternates").is_file()
    assert not any((clone / ".git" / "objects").glob("[0-9a-f][0-9a-f]/*"))
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    environment = module._isolated_git_environment(workspace)

    with module.open_canonical_checkout_root(clone.resolve()) as checkout:
        object_view = module._prepare_isolated_object_view(
            checkout,
            temporary_directory=workspace,
            environment=environment,
        )
        object_type = module._run_git_bytes(
            (f"--git-dir={object_view}", "cat-file", "-t", source_sha),
            cwd=workspace,
            environment=environment,
        )

    assert object_type == b"commit\n"
    alternates = object_view / "objects" / "info" / "alternates"
    alternate_bytes = alternates.read_bytes()
    assert alternate_bytes.endswith(b"\n")
    assert alternate_bytes.count(b"\n") == 1
    assert Path(os.fsdecode(alternate_bytes[:-1])).is_absolute()


def test_isolated_object_view_rejects_newline_in_object_path_without_leaking_it(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    canary = "SECRET-ALTERNATE-PATH-CANARY"
    hostile_parent = tmp_path / f"repository\n{canary}"
    hostile_parent.mkdir()
    repository, _source_sha, _approved_ref = create_provenance_git_repository(
        hostile_parent
    )
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    environment = module._isolated_git_environment(workspace)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError) as captured:
            module._prepare_isolated_object_view(
                checkout,
                temporary_directory=workspace,
                environment=environment,
            )

    assert canary not in str(captured.value)
    assert not any(workspace.glob(".source-object-view-*"))


def test_scoped_tree_parser_preserves_nul_delimited_git_identity() -> None:
    module = load_provenance_module()
    raw = (
        b"040000 tree "
        + b"1" * 40
        + b"\tbackend\0"
        + b"100644 blob "
        + b"2" * 40
        + b"\tbackend/requirements.lock\0"
        + b"040000 tree "
        + b"3" * 40
        + b"\tfrontend\0"
        + b"100755 blob "
        + b"4" * 40
        + "\tfrontend/界面.ts\0".encode()
        + b"040000 tree "
        + b"5" * 40
        + b"\tscripts\0"
        + b"100755 blob "
        + b"6" * 40
        + b"\tscripts/start-hosted.sh\0"
        + b"100644 blob "
        + b"7" * 40
        + b"\t.dockerignore\0"
        + b"100644 blob "
        + b"8" * 40
        + b"\tDockerfile\0"
    )

    entries = module._parse_scoped_tree(raw)

    assert tuple(entries) == (
        "backend",
        "backend/requirements.lock",
        "frontend",
        "frontend/界面.ts",
        "scripts",
        "scripts/start-hosted.sh",
        ".dockerignore",
        "Dockerfile",
    )
    assert entries["backend"].mode == "040000"
    assert entries["backend"].object_type == "tree"
    assert entries["frontend/界面.ts"].mode == "100755"
    assert entries["frontend/界面.ts"].object_id == "4" * 40


@pytest.mark.parametrize(
    "raw",
    (
        b"100644 blob " + b"1" * 40 + b"\tDockerfile",
        b"\0",
        b"100644 blob " + b"1" * 40 + b"Dockerfile\0",
        b"100644  blob " + b"1" * 40 + b"\tDockerfile\0",
        b"100644 blob " + b"A" * 40 + b"\tDockerfile\0",
        b"100644 blob " + b"1" * 39 + b"\tDockerfile\0",
        b"100644 tree " + b"1" * 40 + b"\tDockerfile\0",
        b"040000 blob " + b"1" * 40 + b"\tbackend\0",
        b"120000 blob " + b"1" * 40 + b"\tbackend/link\0",
        b"160000 commit " + b"1" * 40 + b"\tfrontend/module\0",
        b"100644 blob " + b"1" * 40 + b"\tbackend/\xff.py\0",
        b"100644 blob " + b"1" * 40 + b"\tbackend/control\x1f.py\0",
        b"100644 blob " + b"1" * 40 + "\tbackend/control\u0085.py\0".encode(),
        b"100644 blob " + b"1" * 40 + "\tbackend/override\u202e.py\0".encode(),
        b"100644 blob " + b"1" * 40 + b"\t/backend/file.py\0",
        b"100644 blob " + b"1" * 40 + b"\tbackend/../Dockerfile\0",
        b"100644 blob " + b"1" * 40 + b"\tbackend//file.py\0",
        b"100644 blob " + b"1" * 40 + b"\tbackend\\file.py\0",
        b"100644 blob " + b"1" * 40 + b"\tdocs/file.md\0",
        (
            b"100644 blob "
            + b"1" * 40
            + b"\tDockerfile\0"
            + b"100644 blob "
            + b"2" * 40
            + b"\tDockerfile\0"
        ),
    ),
)
def test_scoped_tree_parser_rejects_ambiguous_or_unsafe_records(raw: bytes) -> None:
    module = load_provenance_module()

    with pytest.raises(module.ProvenanceError):
        module._parse_scoped_tree(raw)


def test_scoped_tree_parser_enforces_member_count_before_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    monkeypatch.setattr(module, "MAX_SCOPED_TREE_ENTRIES", 1, raising=False)
    raw = (
        b"040000 tree "
        + b"1" * 40
        + b"\tbackend\0"
        + b"100644 blob "
        + b"2" * 40
        + b"\tbackend/requirements.lock\0"
    )

    with pytest.raises(module.ProvenanceError):
        module._parse_scoped_tree(raw)


def test_blob_collection_enforces_cumulative_bytes_before_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    blob_object_id = hashlib.sha1(b"blob 2\0aa").hexdigest()
    entries = {
        "Dockerfile": module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=blob_object_id,
            path="Dockerfile",
        ),
        ".dockerignore": module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=blob_object_id,
            path=".dockerignore",
        ),
    }
    monkeypatch.setattr(module, "MAX_TOTAL_PROTECTED_BLOB_BYTES", 3, raising=False)
    observed_limits: list[int] = []

    def return_blob(*args: object, **kwargs: object) -> bytes:
        maximum = kwargs["maximum_stdout_bytes"]
        assert isinstance(maximum, int)
        observed_limits.append(maximum)
        return b"aa"

    monkeypatch.setattr(module, "_run_git_bytes", return_blob)

    with pytest.raises(module.ProvenanceError):
        module._collect_blob_bytes(
            entries,
            git_dir="--git-dir=/trusted/object-view",
            workspace=tmp_path,
            environment={},
        )

    assert observed_limits == [3, 1]


def test_blob_collection_rejects_bytes_that_do_not_match_the_tree_object_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    entry = module.TreeEntry(
        mode="100644",
        object_type="blob",
        object_id=hashlib.sha1(b"blob 8\0approved").hexdigest(),
        path="Dockerfile",
    )
    monkeypatch.setattr(module, "_run_git_bytes", lambda *args, **kwargs: b"replaced")

    with pytest.raises(module.ProvenanceError):
        module._collect_blob_bytes(
            {"Dockerfile": entry},
            git_dir="--git-dir=/trusted/object-view",
            workspace=tmp_path,
            environment={},
        )


def test_git_archive_binds_every_scoped_member_to_tree_mode_and_blob_bytes() -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    backend_bytes = b"locked dependency bytes\n"
    dockerfile_bytes = b"FROM scratch\n"
    raw_tree = (
        b"040000 tree "
        + b"1" * 40
        + b"\tbackend\0"
        + b"100644 blob "
        + b"2" * 40
        + b"\tbackend/requirements.lock\0"
        + b"100644 blob "
        + b"3" * 40
        + b"\tDockerfile\0"
    )
    tree = module._parse_scoped_tree(raw_tree)
    archive = build_test_git_archive(
        source_sha,
        (
            ("backend", None, 0o775),
            ("backend/requirements.lock", backend_bytes, 0o664),
            ("Dockerfile", dockerfile_bytes, 0o664),
            ("docs/readme.md", b"outside protected projection\n", 0o664),
        ),
    )

    files = module._validate_git_archive(
        archive,
        source_sha=source_sha,
        tree_entries=tree,
        blob_bytes={
            "backend/requirements.lock": backend_bytes,
            "Dockerfile": dockerfile_bytes,
        },
    )

    assert tuple(files) == ("backend/requirements.lock", "Dockerfile")
    assert files["backend/requirements.lock"].mode == "100644"
    assert files["backend/requirements.lock"].data == backend_bytes
    assert files["Dockerfile"].sha256 == hashlib.sha256(dockerfile_bytes).hexdigest()


def test_archive_extraction_preserves_a_preexisting_destination(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    destination = tmp_path / "existing-context"
    destination.mkdir()
    sentinel = destination / "customer-owned.txt"
    sentinel.write_bytes(b"preserve these bytes\n")
    archive = build_test_git_archive(
        "9" * 40,
        (("Dockerfile", b"FROM scratch\n", 0o664),),
    )

    with pytest.raises(module.ProvenanceError):
        module._extract_validated_git_archive(archive, destination)

    assert destination.is_dir()
    assert sentinel.read_bytes() == b"preserve these bytes\n"


def test_git_archive_accepts_an_exact_canonical_pax_path() -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    path = f"backend/{'a' * 120}.py"
    content = b"approved long-path bytes\n"
    tree = {
        "backend": module.TreeEntry(
            mode="040000",
            object_type="tree",
            object_id="1" * 40,
            path="backend",
        ),
        path: module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content
            ).hexdigest(),
            path=path,
        ),
    }
    archive = build_test_git_archive(
        source_sha,
        (("backend", None, 0o775), (path, content, 0o664)),
    )

    files = module._validate_git_archive(
        archive,
        source_sha=source_sha,
        tree_entries=tree,
        blob_bytes={path: content},
    )

    assert files[path].data == content


def test_git_archive_rejects_a_hidden_gnu_longname_rewrite() -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    path = f"backend/{'a' * 120}.py"
    content = b"approved long-path bytes\n"
    tree = {
        path: module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content
            ).hexdigest(),
            path=path,
        )
    }
    archive = build_test_gnu_longname_archive(
        source_sha,
        name=path,
        content=content,
        mode=0o664,
    )

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            archive,
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={path: content},
        )


@pytest.mark.parametrize("end_blocks", (0, 1))
def test_git_archive_rejects_missing_end_of_archive_blocks(
    end_blocks: int,
) -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    content = b"FROM scratch\n"
    tree = {
        "Dockerfile": module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content
            ).hexdigest(),
            path="Dockerfile",
        )
    }
    complete = build_test_git_archive(
        source_sha,
        (("Dockerfile", content, 0o664),),
    )
    truncated = truncate_tar_to_end_blocks(complete, end_blocks)

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            truncated,
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={"Dockerfile": content},
        )


def test_git_archive_accepts_exactly_two_end_blocks() -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    content = b"FROM scratch\n"
    tree = {
        "Dockerfile": module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content
            ).hexdigest(),
            path="Dockerfile",
        )
    }
    complete = build_test_git_archive(
        source_sha,
        (("Dockerfile", content, 0o664),),
    )
    minimal = truncate_tar_to_end_blocks(complete, 2)

    files = module._validate_git_archive(
        minimal,
        source_sha=source_sha,
        tree_entries=tree,
        blob_bytes={"Dockerfile": content},
    )

    assert files["Dockerfile"].data == content


def test_git_archive_rejects_nonzero_bytes_after_the_end_markers() -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    content = b"FROM scratch\n"
    tree = {
        "Dockerfile": module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id=hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content
            ).hexdigest(),
            path="Dockerfile",
        )
    }
    archive = build_test_git_archive(
        source_sha,
        (("Dockerfile", content, 0o664),),
    )
    archive += b"attacker".ljust(512, b"\0")

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            archive,
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={"Dockerfile": content},
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "duplicate",
        "missing",
        "extra-protected",
        "file-as-directory",
        "directory-as-file",
        "executable-mode",
        "content",
    ),
)
def test_git_archive_rejects_every_scoped_tree_disagreement(
    mutation: str,
) -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    path = "backend/app.py"
    content = b"approved bytes\n"
    tree = {
        "backend": module.TreeEntry(
            mode="040000",
            object_type="tree",
            object_id="1" * 40,
            path="backend",
        ),
        path: module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id="2" * 40,
            path=path,
        ),
    }
    members: list[tuple[str, bytes | None, int]] = [
        ("backend", None, 0o775),
        (path, content, 0o664),
    ]
    if mutation == "duplicate":
        members.append((path, content, 0o664))
    elif mutation == "missing":
        members.pop()
    elif mutation == "extra-protected":
        members.append(("backend/extra.py", b"extra\n", 0o664))
    elif mutation == "file-as-directory":
        members[-1] = (path, None, 0o775)
    elif mutation == "directory-as-file":
        members[0] = ("backend", b"not a directory\n", 0o664)
    elif mutation == "executable-mode":
        members[-1] = (path, content, 0o775)
    elif mutation == "content":
        members[-1] = (path, b"attacker bytes\n", 0o664)
    archive = build_test_git_archive(source_sha, tuple(members))

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            archive,
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={path: content},
        )


@pytest.mark.parametrize(
    "member_type",
    (
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.GNUTYPE_SPARSE,
    ),
)
def test_git_archive_rejects_nonregular_member_types(member_type: bytes) -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    path = "backend/app.py"
    member = tarfile.TarInfo(path)
    member.mode = 0o664
    member.type = member_type
    member.size = 0
    member.linkname = "backend/approved.py"
    member.devmajor = 1
    member.devminor = 3
    archive = build_test_git_archive_from_infos(
        source_sha,
        ((member, None),),
    )
    tree = {
        path: module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id="2" * 40,
            path=path,
        )
    }

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            archive,
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={path: b""},
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "checksum",
        "magic",
        "base256-size",
        "base256-mtime",
        "nul-type",
        "padding",
    ),
)
def test_git_archive_rejects_malformed_raw_headers(mutation: str) -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    path = "Dockerfile"
    content = b"FROM scratch\n"
    archive = bytearray(
        build_test_git_archive(
            source_sha,
            ((path, content, 0o664),),
        )
    )
    member_offset = tar_header_offsets(bytes(archive))[-1]
    if mutation == "checksum":
        archive[member_offset] ^= 1
    elif mutation == "magic":
        archive[member_offset + 257 : member_offset + 263] = b"broken"
        rewrite_test_tar_checksum(archive, member_offset)
    elif mutation == "base256-size":
        archive[member_offset + 124 : member_offset + 136] = b"\x80" + (b"\0" * 11)
        rewrite_test_tar_checksum(archive, member_offset)
    elif mutation == "base256-mtime":
        archive[member_offset + 136 : member_offset + 148] = (
            b"\x80" + (b"\0" * 10) + b"\1"
        )
        rewrite_test_tar_checksum(archive, member_offset)
    elif mutation == "nul-type":
        archive[member_offset + 156] = 0
        rewrite_test_tar_checksum(archive, member_offset)
    elif mutation == "padding":
        archive[member_offset + tarfile.BLOCKSIZE + len(content)] = 1
    tree = {
        path: module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id="2" * 40,
            path=path,
        )
    }

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            bytes(archive),
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={path: content},
        )


@pytest.mark.parametrize("pax_mutation", ("unknown-key", "mismatched-path"))
def test_git_archive_rejects_unapproved_local_pax_metadata(
    pax_mutation: str,
) -> None:
    module = load_provenance_module()
    source_sha = "9" * 40
    path = "backend/app.py"
    member = tarfile.TarInfo(path)
    member.mode = 0o664
    member.size = len(b"approved\n")
    if pax_mutation == "unknown-key":
        member.pax_headers = {"path": path, "attacker": "value"}
    else:
        member.pax_headers = {"path": "backend/other.py"}
    archive = build_test_git_archive_from_infos(
        source_sha,
        ((member, b"approved\n"),),
    )
    tree = {
        path: module.TreeEntry(
            mode="100644",
            object_type="blob",
            object_id="2" * 40,
            path=path,
        )
    }

    with pytest.raises(module.ProvenanceError):
        module._validate_git_archive(
            archive,
            source_sha=source_sha,
            tree_entries=tree,
            blob_bytes={path: b"approved\n"},
        )


@pytest.mark.parametrize(
    ("component", "entries", "expected_bytes", "expected_digest"),
    (
        (
            "backend",
            (),
            b'{"component":"backend","entries":[],"schema":1}',
            "ebb741d633cd371d7ea4820fd535860f3f45b896a3704d71472546e9c68abd16",
        ),
        (
            "backend",
            (("backend/app.py", "100644", "1" * 64),),
            (
                b'{"component":"backend","entries":[{"mode":"100644",'
                b'"path":"backend/app.py","sha256":"' + b"1" * 64 + b'"}],"schema":1}'
            ),
            "788d9f08481b50466f4d2dcaad836f5cbdf09c92e719a9c31f3c2d1eb99f98d0",
        ),
        (
            "frontend",
            (
                ("frontend/界面.ts", "100755", "3" * 64),
                ("frontend/a.ts", "100644", "2" * 64),
            ),
            (
                b'{"component":"frontend","entries":[{"mode":"100644",'
                b'"path":"frontend/a.ts","sha256":"'
                + b"2"
                * 64
                + b'"},{"mode":"100755","path":"frontend/\\u754c\\u9762.ts",'
                b'"sha256":"' + b"3" * 64 + b'"}],"schema":1}'
            ),
            "37954085eb1b8eb44898638ddfb176c328973ed43656bb95702159bd66a6e4fd",
        ),
    ),
)
def test_runtime_component_inventory_has_pinned_canonical_bytes_and_digest(
    component: str,
    entries: tuple[tuple[str, str, str], ...],
    expected_bytes: bytes,
    expected_digest: str,
) -> None:
    module = load_provenance_module()
    inventory_entries = tuple(
        module.RuntimeInventoryEntry(path=path, mode=mode, sha256=digest)
        for path, mode, digest in entries
    )

    evidence = module._canonical_runtime_component(component, inventory_entries)

    assert evidence.canonical_bytes == expected_bytes
    assert evidence.sha256 == expected_digest


def test_dockerignore_and_combined_inventory_have_pinned_domains() -> None:
    module = load_provenance_module()

    dockerignore = module._canonical_dockerignore_component(b"node_modules\n")
    combined = module._canonical_combined_runtime_inventory(
        {
            "backend": "a" * 64,
            "dockerignore": "b" * 64,
            "frontend": "c" * 64,
            "scripts": "d" * 64,
        }
    )

    assert dockerignore.canonical_bytes == (
        b'{"component":"dockerignore","schema":1,"sha256":'
        b'"16d30e4462189fb14dd611bdb708c510630c576a1f35b9383e89a4352da36c97"}'
    )
    assert (
        dockerignore.sha256
        == "77f4e26a0044097d09e7f4b40c06b26f4cfcf7e6549f7505386569a1050e8e81"
    )
    assert combined.canonical_bytes == (
        b'{"components":{"backend":"'
        + b"a" * 64
        + b'","dockerignore":"'
        + b"b" * 64
        + b'","frontend":"'
        + b"c" * 64
        + b'","scripts":"'
        + b"d" * 64
        + b'"},"policy":"runtime-copy-inputs-v1","schema":1}'
    )
    assert (
        combined.sha256
        == "b3b1f9868a0e8ca6fb608f7752d057a280e381d45925e831b2c00f70d0b5713a"
    )


def test_runtime_inventory_applies_only_the_fixed_exclusions_and_requires_anchors() -> (
    None
):
    module = load_provenance_module()

    def archived(path: str, content: bytes, mode: str = "100644") -> object:
        return module.ArchiveFile(
            path=path,
            mode=mode,
            data=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    files = {
        ".dockerignore": archived(".dockerignore", b"locked ignore policy\n"),
        "Dockerfile": archived("Dockerfile", b"FROM scratch\n"),
        "backend/requirements.lock": archived(
            "backend/requirements.lock", b"backend lock\n"
        ),
        "frontend/package.json": archived("frontend/package.json", b"{}\n"),
        "frontend/package-lock.json": archived(
            "frontend/package-lock.json", b'{"lockfileVersion":3}\n'
        ),
        "scripts/start-hosted.sh": archived(
            "scripts/start-hosted.sh", b"#!/bin/sh\n", "100755"
        ),
    }
    excluded_prefixes = (
        "backend/.mypy_cache/",
        "backend/.pytest_cache/",
        "backend/.ruff_cache/",
        "backend/__pycache__/",
        "backend/wheelhouse/",
        "backend/postgres_tests/",
        "backend/tests/",
        "frontend/coverage/",
        "frontend/dist/",
        "frontend/node_modules/",
        "frontend/playwright-report/",
        "frontend/test-results/",
        "scripts/tests/",
    )
    for index, prefix in enumerate(excluded_prefixes):
        path = f"{prefix}excluded-{index}.txt"
        files[path] = archived(path, b"excluded\n")
    for suffix in (".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".pyc"):
        path = f"backend/live/excluded{suffix}"
        files[path] = archived(path, b"excluded by basename\n")
    included_paths = {
        "backend/tests-near-miss/keep.py",
        "backend/live/data.DB",
        "backend/live/data.db.keep",
        "frontend/distinct/keep.ts",
        "scripts/tests-near-miss/keep.sh",
    }
    for path in included_paths:
        files[path] = archived(path, f"included {path}\n".encode())

    inventory = module._build_runtime_inventory(files)

    observed_paths: set[str] = set()
    for component in ("backend", "frontend", "scripts"):
        payload = json.loads(inventory.components[component].canonical_bytes)
        observed_paths.update(entry["path"] for entry in payload["entries"])
    assert included_paths <= observed_paths
    assert all(
        not any(path.startswith(prefix) for prefix in excluded_prefixes)
        for path in observed_paths
    )
    assert not any(
        path.endswith((".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".pyc"))
        for path in observed_paths
    )
    assert inventory.components["dockerignore"].canonical_bytes.startswith(
        b'{"component":"dockerignore"'
    )
    assert (
        inventory.combined.sha256
        == module._canonical_combined_runtime_inventory(
            {name: component.sha256 for name, component in inventory.components.items()}
        ).sha256
    )


@pytest.mark.parametrize(
    "missing_anchor",
    (
        "backend/requirements.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "scripts/start-hosted.sh",
        ".dockerignore",
    ),
)
def test_runtime_inventory_rejects_a_missing_required_anchor(
    missing_anchor: str,
) -> None:
    module = load_provenance_module()

    def archived(path: str) -> object:
        content = f"{path}\n".encode()
        return module.ArchiveFile(
            path=path,
            mode="100755" if path == "scripts/start-hosted.sh" else "100644",
            data=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    paths = {
        ".dockerignore",
        "backend/requirements.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "scripts/start-hosted.sh",
    }
    paths.remove(missing_anchor)

    with pytest.raises(module.ProvenanceError):
        module._build_runtime_inventory({path: archived(path) for path in paths})


@pytest.mark.parametrize(
    ("mutation", "changed_component"),
    (
        ("add", "backend"),
        ("delete", "backend"),
        ("rename", "backend"),
        ("content", "backend"),
        ("mode", "backend"),
        ("requirements-lock", "backend"),
        ("package-json", "frontend"),
        ("package-lock", "frontend"),
        ("included-script", "scripts"),
        ("dockerignore", "dockerignore"),
    ),
)
def test_runtime_inventory_binds_every_included_copy_input_mutation(
    mutation: str,
    changed_component: str,
) -> None:
    module = load_provenance_module()

    def archived(path: str, content: bytes, mode: str = "100644") -> object:
        return module.ArchiveFile(
            path=path,
            mode=mode,
            data=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    files = {
        ".dockerignore": archived(".dockerignore", b"locked ignore policy\n"),
        "backend/requirements.lock": archived(
            "backend/requirements.lock", b"backend==1\n"
        ),
        "backend/app.py": archived("backend/app.py", b"print('approved')\n"),
        "frontend/package.json": archived("frontend/package.json", b"{}\n"),
        "frontend/package-lock.json": archived(
            "frontend/package-lock.json", b'{"lockfileVersion":3}\n'
        ),
        "frontend/src/app.ts": archived(
            "frontend/src/app.ts", b"export const approved = true;\n"
        ),
        "scripts/start-hosted.sh": archived(
            "scripts/start-hosted.sh", b"#!/bin/sh\n", "100755"
        ),
        "scripts/reconcile.py": archived(
            "scripts/reconcile.py", b"print('approved')\n"
        ),
    }
    baseline = module._build_runtime_inventory(files)
    changed = dict(files)
    if mutation == "add":
        changed["backend/new.py"] = archived("backend/new.py", b"new\n")
    elif mutation == "delete":
        del changed["backend/app.py"]
    elif mutation == "rename":
        changed["backend/renamed.py"] = replace(
            changed.pop("backend/app.py"),
            path="backend/renamed.py",
        )
    elif mutation == "content":
        changed["backend/app.py"] = archived("backend/app.py", b"print('changed')\n")
    elif mutation == "mode":
        changed["backend/app.py"] = archived(
            "backend/app.py", b"print('approved')\n", "100755"
        )
    elif mutation == "requirements-lock":
        changed["backend/requirements.lock"] = archived(
            "backend/requirements.lock", b"backend==2\n"
        )
    elif mutation == "package-json":
        changed["frontend/package.json"] = archived(
            "frontend/package.json", b'{"changed":true}\n'
        )
    elif mutation == "package-lock":
        changed["frontend/package-lock.json"] = archived(
            "frontend/package-lock.json", b'{"lockfileVersion":4}\n'
        )
    elif mutation == "included-script":
        changed["scripts/reconcile.py"] = archived(
            "scripts/reconcile.py", b"print('changed')\n"
        )
    elif mutation == "dockerignore":
        changed[".dockerignore"] = archived(".dockerignore", b"changed ignore policy\n")

    candidate = module._build_runtime_inventory(changed)

    assert (
        candidate.components[changed_component].sha256
        != baseline.components[changed_component].sha256
    )
    assert candidate.combined.sha256 != baseline.combined.sha256


@pytest.mark.parametrize(
    "excluded_path",
    (
        "docs/readme.md",
        "deploy/workstation/tool.py",
        "backend/tests/test_app.py",
        "backend/.mypy_cache/state.json",
        "frontend/dist/bundle.js",
        "scripts/tests/test_tool.py",
        "backend/live/cache.pyc",
    ),
)
def test_runtime_inventory_ignores_only_documented_nonruntime_changes(
    excluded_path: str,
) -> None:
    module = load_provenance_module()

    def archived(path: str, content: bytes, mode: str = "100644") -> object:
        return module.ArchiveFile(
            path=path,
            mode=mode,
            data=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    files = {
        ".dockerignore": archived(".dockerignore", b"locked ignore policy\n"),
        "backend/requirements.lock": archived(
            "backend/requirements.lock", b"backend==1\n"
        ),
        "frontend/package.json": archived("frontend/package.json", b"{}\n"),
        "frontend/package-lock.json": archived(
            "frontend/package-lock.json", b'{"lockfileVersion":3}\n'
        ),
        "scripts/start-hosted.sh": archived(
            "scripts/start-hosted.sh", b"#!/bin/sh\n", "100755"
        ),
        excluded_path: archived(excluded_path, b"before\n"),
    }
    baseline = module._build_runtime_inventory(files)
    files[excluded_path] = archived(excluded_path, b"after\n", "100755")

    candidate = module._build_runtime_inventory(files)

    assert candidate == baseline


def test_export_revision_evidence_uses_only_the_approved_git_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    repository = tmp_path / "repository"
    run_test_git(
        clone_parent,
        "clone",
        "--shared",
        str(PRODUCT_ROOT),
        str(repository),
    )
    run_test_git(repository, "config", "user.name", "Provenance Test")
    run_test_git(
        repository,
        "config",
        "user.email",
        "provenance-test@users.noreply.github.com",
    )
    source_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    approved_ref = "refs/remotes/origin/approved"
    run_test_git(repository, "update-ref", approved_ref, source_sha)
    approved_backend = run_test_git(
        repository, "show", f"{source_sha}:backend/app/main.py"
    )
    (repository / "backend" / "app" / "main.py").write_text(
        "SECRET-DIRTY-WORKTREE\n",
        encoding="utf-8",
    )
    run_test_git(repository, "add", "backend/app/main.py")
    run_test_git(repository, "commit", "--message", "hostile replacement")
    replacement_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    run_test_git(repository, "replace", source_sha, replacement_sha)
    (repository / ".git" / "info" / "attributes").write_text(
        "backend/requirements.lock export-ignore\n",
        encoding="utf-8",
    )
    attacker_bin = tmp_path / "attacker-bin"
    attacker_bin.mkdir()
    marker = tmp_path / "attacker-git-ran"
    fake_git = attacker_bin / "git"
    fake_git.write_text(
        f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 91\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{attacker_bin}:/usr/bin:/bin")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "attacker-tree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.attributesFile")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(repository / ".git/info/attributes"))
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    context = tmp_path / "context"

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        evidence = module.export_revision_evidence(
            checkout,
            source_sha,
            approved_ref,
            workspace=workspace,
            extract_to=context,
        )

    assert evidence.source_sha == source_sha
    assert (
        evidence.dockerfile_blob_sha
        == run_test_git(repository, "ls-tree", source_sha, "--", "Dockerfile")
        .decode("ascii")
        .split()[2]
    )
    approved_dockerfile = run_test_git(repository, "show", f"{source_sha}:Dockerfile")
    assert evidence.dockerfile_sha256 == hashlib.sha256(approved_dockerfile).hexdigest()
    assert set(evidence.runtime_inventory.components) == {
        "backend",
        "dockerignore",
        "frontend",
        "scripts",
    }
    assert evidence.runtime_inventory.combined.sha256
    for asset in DEPLOYMENT_ASSET_DIGESTS:
        approved_asset = run_test_git(repository, "show", f"{source_sha}:{asset}")
        assert (
            evidence.deployment_assets.sha256[asset]
            == hashlib.sha256(approved_asset).hexdigest()
        )
    assert (context / "backend" / "app" / "main.py").read_bytes() == approved_backend
    assert not marker.exists()
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    ("attribute_rule", "mutate_dockerfile"),
    (
        ("backend/requirements.lock export-ignore\n", False),
        ("Dockerfile export-subst\n", True),
    ),
)
def test_export_revision_evidence_rejects_protected_archive_transformations(
    tmp_path: Path,
    attribute_rule: str,
    mutate_dockerfile: bool,
) -> None:
    module = load_provenance_module()
    repository, _source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    if mutate_dockerfile:
        (repository / "Dockerfile").write_text(
            "FROM scratch\n# $Format:%H$\n",
            encoding="utf-8",
        )
    (repository / ".gitattributes").write_text(attribute_rule, encoding="utf-8")
    run_test_git(repository, "add", ".gitattributes", "Dockerfile")
    run_test_git(repository, "commit", "--message", "hostile export policy")
    source_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    run_test_git(repository, "update-ref", approved_ref, source_sha)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.export_revision_evidence(
                checkout,
                source_sha,
                approved_ref,
                workspace=workspace,
            )

    assert list(workspace.iterdir()) == []


def test_export_revision_rejects_runtime_symlink_before_archive_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    repository, _source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    (repository / "backend" / "link.py").symlink_to("requirements.lock")
    run_test_git(repository, "add", "backend/link.py")
    run_test_git(repository, "commit", "--message", "runtime symlink")
    source_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    run_test_git(repository, "update-ref", approved_ref, source_sha)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    real_run_git = module._run_git_bytes
    observed_arguments: list[tuple[str, ...]] = []

    def record_git(arguments: tuple[str, ...], **kwargs: object) -> bytes:
        observed_arguments.append(arguments)
        return real_run_git(arguments, **kwargs)

    monkeypatch.setattr(module, "_run_git_bytes", record_git)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.export_revision_evidence(
                checkout,
                source_sha,
                approved_ref,
                workspace=workspace,
            )

    assert not any("archive" in arguments for arguments in observed_arguments)
    assert list(workspace.iterdir()) == []


def test_export_revision_evidence_hashes_exported_deployment_asset_bytes(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    repository, _source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    asset_path = "deploy/workstation/compose.yaml"
    raw_asset = b"services: {}\n# $Format:%H$\n"
    (repository / asset_path).write_bytes(raw_asset)
    (repository / ".gitattributes").write_text(
        f"{asset_path} export-subst\n",
        encoding="utf-8",
    )
    run_test_git(repository, "add", ".gitattributes", asset_path)
    run_test_git(repository, "commit", "--message", "export deployment identity")
    source_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    run_test_git(repository, "update-ref", approved_ref, source_sha)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        evidence = module.export_revision_evidence(
            checkout,
            source_sha,
            approved_ref,
            workspace=workspace,
        )

    exported_asset = f"services: {{}}\n# {source_sha}\n".encode()
    assert (
        evidence.deployment_assets.sha256[asset_path]
        == hashlib.sha256(exported_asset).hexdigest()
    )
    assert (
        evidence.deployment_assets.sha256[asset_path]
        != hashlib.sha256(raw_asset).hexdigest()
    )


def test_export_revision_evidence_verifies_deployment_asset_blob_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    repository, source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    asset_path = "deploy/workstation/compose.yaml"
    asset_object_id = (
        run_test_git(repository, "rev-parse", f"{source_sha}:{asset_path}")
        .decode("ascii")
        .removesuffix("\n")
    )
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    real_run_git = module._run_git_bytes

    def return_corrupt_asset_blob(
        arguments: tuple[str, ...],
        **kwargs: object,
    ) -> bytes:
        if arguments[-3:] == ("cat-file", "blob", asset_object_id):
            return b"content whose Git object ID does not match the tree\n"
        return real_run_git(arguments, **kwargs)

    monkeypatch.setattr(module, "_run_git_bytes", return_corrupt_asset_blob)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.export_revision_evidence(
                checkout,
                source_sha,
                approved_ref,
                workspace=workspace,
            )

    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("operation", ("export", "equivalence"))
@pytest.mark.parametrize("workspace_shape", ("missing", "symlink-loop"))
def test_git_evidence_apis_sanitize_an_unresolvable_workspace(
    tmp_path: Path,
    operation: str,
    workspace_shape: str,
) -> None:
    module = load_provenance_module()
    repository, source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    workspace = tmp_path / "unresolvable-workspace"
    if workspace_shape == "symlink-loop":
        workspace.symlink_to(workspace.name)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            if operation == "export":
                module.export_revision_evidence(
                    checkout,
                    source_sha,
                    approved_ref,
                    workspace=workspace,
                )
            else:
                module.recompute_runtime_equivalence(
                    checkout,
                    donor_sha=source_sha,
                    target_sha=source_sha,
                    approved_remote_ref=approved_ref,
                    workspace=workspace,
                )


def test_export_revision_evidence_removes_its_context_if_final_ref_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    repository, source_sha, approved_ref = create_provenance_git_repository(tmp_path)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    context = tmp_path / "context"
    real_export = module._export_revision_evidence_from_commit

    def export_then_move_ref(*args: object, **kwargs: object) -> object:
        result = real_export(*args, **kwargs)
        run_test_git(repository, "update-ref", "-d", approved_ref)
        return result

    monkeypatch.setattr(
        module,
        "_export_revision_evidence_from_commit",
        export_then_move_ref,
    )

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.export_revision_evidence(
                checkout,
                source_sha,
                approved_ref,
                workspace=workspace,
                extract_to=context,
            )

    assert not context.exists()
    assert list(workspace.iterdir()) == []


def test_recompute_runtime_equivalence_accepts_the_reviewed_real_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    repository = tmp_path / "repository"
    run_test_git(
        clone_parent,
        "clone",
        "--shared",
        str(PRODUCT_ROOT),
        str(repository),
    )
    target_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    approved_ref = "refs/remotes/origin/approved"
    run_test_git(repository, "update-ref", approved_ref, target_sha)
    attacker_bin = tmp_path / "attacker-bin"
    attacker_bin.mkdir()
    marker = tmp_path / "attacker-git-ran"
    fake_git = attacker_bin / "git"
    fake_git.write_text(
        f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 91\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{attacker_bin}:/usr/bin:/bin")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "attacker-tree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.attributesFile")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "attacker-attributes"))
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        evidence = module.recompute_runtime_equivalence(
            checkout,
            donor_sha="bf78fe34d120e305baa151a97fb351235a0d37c4",
            target_sha=target_sha,
            approved_remote_ref=approved_ref,
            workspace=workspace,
        )

    assert evidence.donor.source_sha == ("bf78fe34d120e305baa151a97fb351235a0d37c4")
    assert evidence.target.source_sha == target_sha
    equivalence = evidence.runtime_equivalence
    expected_components = {
        "backend": "a270018406818c487bba6ef73a69852116f7d2cba25474d65660d73b1a8116c7",
        "dockerignore": (
            "3fa6f8405268fac4ca288ebc6288f2baf7e242fe1220b4e6aa3d47c267d22623"
        ),
        "frontend": "614988fc815418e919e4cab8615603701069804d2501329761118c60f5811419",
        "scripts": "0acec4901754662ccd41f3afbf0e0914948398ef5b5a3d3e390a49dbe4053b1f",
    }
    expected_combined = (
        "1968df4c02956a99e9e2328d7707a69988861efe1296317ad874e3a3cfa51d6a"
    )
    assert {
        name: component.sha256
        for name, component in evidence.donor.runtime_inventory.components.items()
    } == expected_components
    assert {
        name: component.sha256
        for name, component in evidence.target.runtime_inventory.components.items()
    } == expected_components
    assert evidence.donor.runtime_inventory.combined.sha256 == expected_combined
    assert evidence.target.runtime_inventory.combined.sha256 == expected_combined
    assert not marker.exists()
    assert list(workspace.iterdir()) == []
    assert equivalence.schema == 1
    assert equivalence.policy == "runtime-copy-inputs-v1+pip-timeout-300-v1"
    assert equivalence.donor_copy_inputs_sha256 == equivalence.target_copy_inputs_sha256
    assert all(
        pair.donor_sha256 == pair.target_sha256
        for pair in equivalence.components.values()
    )
    assert equivalence.dockerfile_transition == module.DockerfileTransition(
        policy="pip-timeout-300-v1",
        from_sha256=(
            "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
        ),
        to_sha256=("35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"),
    )
    with pytest.raises(FrozenInstanceError):
        evidence.donor.source_sha = "0" * 40
    with pytest.raises(TypeError):
        evidence.donor.deployment_assets.sha256["attacker"] = "0" * 64
    with pytest.raises(TypeError):
        evidence.donor.runtime_inventory.components["backend"] = (
            evidence.donor.runtime_inventory.components["frontend"]
        )
    with pytest.raises(TypeError):
        equivalence.components["backend"] = equivalence.components["frontend"]


def test_runtime_equivalence_rejects_a_digest_with_tampered_canonical_bytes() -> None:
    module = load_provenance_module()
    donor = make_policy_revision(
        module,
        "1" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="1d697b8fe13af6b0f787fe5639568b97aa3973de",
        dockerfile_sha256=(
            "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
        ),
    )
    target = make_policy_revision(
        module,
        "2" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa",
        dockerfile_sha256=(
            "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
        ),
    )
    target_components = dict(target.runtime_inventory.components)
    original = target_components["backend"]
    target_components["backend"] = replace(
        original,
        canonical_bytes=b"tampered canonical bytes",
    )
    tampered_target = replace(
        target,
        runtime_inventory=replace(
            target.runtime_inventory,
            components=target_components,
        ),
    )

    with pytest.raises(module.ProvenanceError):
        module._runtime_equivalence_from_revisions(donor, tampered_target)


def test_runtime_equivalence_rejects_malformed_nested_evidence_cleanly() -> None:
    module = load_provenance_module()
    donor = make_policy_revision(
        module,
        "1" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="1d697b8fe13af6b0f787fe5639568b97aa3973de",
        dockerfile_sha256=(
            "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
        ),
    )
    target = make_policy_revision(
        module,
        "2" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa",
        dockerfile_sha256=(
            "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
        ),
    )
    malformed = replace(donor, runtime_inventory=object())

    with pytest.raises(module.ProvenanceError):
        module._runtime_equivalence_from_revisions(malformed, target)


def test_runtime_equivalence_rejects_runtime_drift_and_every_other_recipe_pair() -> (
    None
):
    module = load_provenance_module()
    donor = make_policy_revision(
        module,
        "1" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="1d697b8fe13af6b0f787fe5639568b97aa3973de",
        dockerfile_sha256=(
            "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
        ),
    )
    target = make_policy_revision(
        module,
        "2" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa",
        dockerfile_sha256=(
            "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
        ),
    )

    accepted = module._runtime_equivalence_from_revisions(donor, target)

    assert accepted.donor_copy_inputs_sha256 == accepted.target_copy_inputs_sha256
    drifted_target = make_policy_revision(
        module,
        "2" * 40,
        dockerfile_mode="100644",
        dockerfile_blob_sha="b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa",
        dockerfile_sha256=(
            "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
        ),
        component_suffix=b"runtime drift",
    )
    donor_with_target_recipe = replace(
        donor,
        dockerfile_blob_sha="b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa",
        dockerfile_sha256=(
            "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
        ),
    )
    target_with_donor_recipe = replace(
        target,
        dockerfile_blob_sha="1d697b8fe13af6b0f787fe5639568b97aa3973de",
        dockerfile_sha256=(
            "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
        ),
    )
    third_recipe_donor = replace(
        donor,
        dockerfile_blob_sha="c" * 40,
        dockerfile_sha256="d" * 64,
    )
    third_recipe_target = replace(
        target,
        dockerfile_blob_sha="e" * 40,
        dockerfile_sha256="f" * 64,
    )
    rejected_pairs = (
        (donor, drifted_target),
        (donor, replace(target, dockerfile_mode="100755")),
        (donor, replace(target, dockerfile_blob_sha="f" * 40)),
        (donor, replace(target, dockerfile_sha256="f" * 64)),
        (replace(donor, dockerfile_mode="100755"), target),
        (replace(donor, dockerfile_blob_sha="e" * 40), target),
        (replace(donor, dockerfile_sha256="e" * 64), target),
        (donor_with_target_recipe, target),
        (donor, target_with_donor_recipe),
        (donor_with_target_recipe, target_with_donor_recipe),
        (third_recipe_donor, third_recipe_target),
    )
    for candidate_donor, candidate_target in rejected_pairs:
        with pytest.raises(module.ProvenanceError):
            module._runtime_equivalence_from_revisions(
                candidate_donor,
                candidate_target,
            )


def test_recompute_runtime_equivalence_requires_strict_baseline_descent(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    repository = tmp_path / "repository"
    run_test_git(
        clone_parent,
        "clone",
        "--shared",
        str(PRODUCT_ROOT),
        str(repository),
    )
    baseline = "1a333c8a3ae5172f4b025e4543386debec826cbf"
    approved_ref = "refs/remotes/origin/approved"
    run_test_git(repository, "update-ref", approved_ref, baseline)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.recompute_runtime_equivalence(
                checkout,
                donor_sha="bf78fe34d120e305baa151a97fb351235a0d37c4",
                target_sha=baseline,
                approved_remote_ref=approved_ref,
                workspace=workspace,
            )


def test_recompute_runtime_equivalence_rejects_unapproved_ancestry_shapes(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    repository = tmp_path / "repository"
    run_test_git(
        clone_parent,
        "clone",
        "--shared",
        str(PRODUCT_ROOT),
        str(repository),
    )
    run_test_git(repository, "config", "user.name", "Provenance Test")
    run_test_git(repository, "config", "user.email", TEST_NOREPLY_EMAIL)
    target_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    baseline = "1a333c8a3ae5172f4b025e4543386debec826cbf"
    before_baseline = (
        run_test_git(repository, "rev-parse", f"{baseline}^")
        .decode("ascii")
        .removesuffix("\n")
    )
    tree_object = (
        run_test_git(repository, "rev-parse", f"{target_sha}^{{tree}}")
        .decode("ascii")
        .removesuffix("\n")
    )
    sibling = (
        run_test_git(repository, "commit-tree", tree_object, "-m", "sibling")
        .decode("ascii")
        .removesuffix("\n")
    )
    noncommit = (
        run_test_git(repository, "rev-parse", f"{target_sha}:.dockerignore")
        .decode("ascii")
        .removesuffix("\n")
    )
    donor = "bf78fe34d120e305baa151a97fb351235a0d37c4"
    approved_ref = "refs/remotes/origin/approved"
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    cases = (
        ("0" * 40, target_sha),
        (noncommit, target_sha),
        (target_sha, target_sha),
        (sibling, target_sha),
        (donor, before_baseline),
        (donor, sibling),
    )

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        for candidate_donor, candidate_target in cases:
            run_test_git(repository, "update-ref", approved_ref, candidate_target)
            with pytest.raises(module.ProvenanceError):
                module.recompute_runtime_equivalence(
                    checkout,
                    donor_sha=candidate_donor,
                    target_sha=candidate_target,
                    approved_remote_ref=approved_ref,
                    workspace=workspace,
                )
            assert list(workspace.iterdir()) == []


def test_recompute_runtime_equivalence_ignores_legacy_grafts(
    tmp_path: Path,
) -> None:
    module = load_provenance_module()
    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    repository = tmp_path / "repository"
    run_test_git(
        clone_parent,
        "clone",
        "--shared",
        str(PRODUCT_ROOT),
        str(repository),
    )
    run_test_git(repository, "config", "user.name", "Provenance Test")
    run_test_git(repository, "config", "user.email", TEST_NOREPLY_EMAIL)
    tree_object = (
        run_test_git(repository, "rev-parse", "HEAD^{tree}")
        .decode("ascii")
        .removesuffix("\n")
    )
    unrelated_target = (
        run_test_git(repository, "commit-tree", tree_object, "-m", "unrelated target")
        .decode("ascii")
        .removesuffix("\n")
    )
    donor = "bf78fe34d120e305baa151a97fb351235a0d37c4"
    baseline = "1a333c8a3ae5172f4b025e4543386debec826cbf"
    (repository / ".git" / "info" / "grafts").write_text(
        f"{unrelated_target} {donor} {baseline}\n",
        encoding="ascii",
    )
    approved_ref = "refs/remotes/origin/approved"
    run_test_git(repository, "update-ref", approved_ref, unrelated_target)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.recompute_runtime_equivalence(
                checkout,
                donor_sha=donor,
                target_sha=unrelated_target,
                approved_remote_ref=approved_ref,
                workspace=workspace,
            )

    assert list(workspace.iterdir()) == []


def test_recompute_runtime_equivalence_rejects_final_approved_ref_movement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_provenance_module()
    clone_parent = tmp_path / "clone-parent"
    clone_parent.mkdir()
    repository = tmp_path / "repository"
    run_test_git(
        clone_parent,
        "clone",
        "--shared",
        str(PRODUCT_ROOT),
        str(repository),
    )
    target_sha = (
        run_test_git(repository, "rev-parse", "HEAD").decode("ascii").removesuffix("\n")
    )
    approved_ref = "refs/remotes/origin/approved"
    run_test_git(repository, "update-ref", approved_ref, target_sha)
    workspace = tmp_path / "git-workspace"
    workspace.mkdir(mode=0o700)
    real_export = module.export_revision_evidence

    def export_then_move_ref(*args: object, **kwargs: object) -> object:
        result = real_export(*args, **kwargs)
        run_test_git(
            repository,
            "update-ref",
            approved_ref,
            "1a333c8a3ae5172f4b025e4543386debec826cbf",
        )
        return result

    monkeypatch.setattr(module, "export_revision_evidence", export_then_move_ref)

    with module.open_canonical_checkout_root(repository.resolve()) as checkout:
        with pytest.raises(module.ProvenanceError):
            module.recompute_runtime_equivalence(
                checkout,
                donor_sha="bf78fe34d120e305baa151a97fb351235a0d37c4",
                target_sha=target_sha,
                approved_remote_ref=approved_ref,
                workspace=workspace,
            )

    assert list(workspace.iterdir()) == []


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
