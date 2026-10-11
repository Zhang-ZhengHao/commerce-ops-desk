"""Shared provenance primitives for verified builds and deployments."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import io
import json
import math
import os
import pwd
import re
import secrets
import selectors
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, Self, cast

LEGACY_MANIFEST_FIELDS = frozenset(
    {
        "approved_remote_ref",
        "deployment_assets",
        "docker_daemon_id",
        "image_id",
        "image_reference",
        "schema",
        "source_sha",
    }
)
SCHEMA3_MANIFEST_FIELDS = LEGACY_MANIFEST_FIELDS | {"build"}
SOURCE_BUILD_FIELDS = frozenset({"kind", "schema"})
REUSE_BUILD_FIELDS = frozenset({"kind", "reused_from", "runtime_equivalence", "schema"})
CANDIDATE_SOURCE_PROVENANCE_FIELDS = frozenset(
    {
        "kind",
        "schema",
        "source_manifest_schema",
        "source_manifest_sha256",
    }
)
CANDIDATE_REUSE_PROVENANCE_FIELDS = REUSE_BUILD_FIELDS | {"source_manifest_sha256"}
REUSED_FROM_FIELDS = frozenset(
    {"image_id", "image_reference", "manifest_sha256", "source_sha"}
)
RUNTIME_EQUIVALENCE_FIELDS = frozenset(
    {
        "components",
        "dockerfile_transition",
        "donor_copy_inputs_sha256",
        "policy",
        "schema",
        "target_copy_inputs_sha256",
    }
)
RUNTIME_COMPONENTS = frozenset({"backend", "dockerignore", "frontend", "scripts"})
DIGEST_PAIR_FIELDS = frozenset({"donor_sha256", "target_sha256"})
DOCKERFILE_TRANSITION_FIELDS = frozenset({"from_sha256", "policy", "to_sha256"})
DEPLOYMENT_ASSET_FIELDS = frozenset({"schema", "sha256"})
DEPLOYMENT_ASSET_PATHS = frozenset(
    {
        "deploy/workstation/compose.yaml",
        "deploy/workstation/commerce-ops-desk.Caddyfile.template",
        "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template",
    }
)
ARCHIVE_ATTRIBUTE_PATHS = frozenset(
    {
        ".gitattributes",
        "deploy/.gitattributes",
        "deploy/workstation/.gitattributes",
    }
)
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_NAME_PATTERN = re.compile(r"build-([0-9a-f]{40})\.json\Z")
RUNTIME_EQUIVALENCE_POLICY = "runtime-copy-inputs-v1+pip-timeout-300-v1"
RUNTIME_COPY_INPUTS_POLICY = "runtime-copy-inputs-v1"
DOCKERFILE_TRANSITION_POLICY = "pip-timeout-300-v1"
CANDIDATE_BUILD_PROVENANCE_DOMAIN = b"commerce-ops-desk:candidate-build-provenance:v1\0"
RESERVED_PROVENANCE_LABEL_PREFIX = "io.github.zhang-zhenghao.commerce-ops."
BUILD_KIND_LABEL = f"{RESERVED_PROVENANCE_LABEL_PREFIX}build-kind"
REUSED_FROM_SOURCE_LABEL = f"{RESERVED_PROVENANCE_LABEL_PREFIX}reused-from-source"
REUSED_FROM_IMAGE_ID_LABEL = f"{RESERVED_PROVENANCE_LABEL_PREFIX}reused-from-image-id"
RUNTIME_INPUTS_SHA256_LABEL = f"{RESERVED_PROVENANCE_LABEL_PREFIX}runtime-inputs-sha256"
DONOR_DOCKERFILE_SHA256 = (
    "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
)
TARGET_DOCKERFILE_SHA256 = (
    "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
)
DONOR_DOCKERFILE_BLOB_SHA = "1d697b8fe13af6b0f787fe5639568b97aa3973de"
TARGET_DOCKERFILE_BLOB_SHA = "b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa"
OBSERVED_BASELINE_SHA = "1a333c8a3ae5172f4b025e4543386debec826cbf"
GIT_BINARY = "/usr/bin/git"
SAFE_SYSTEM_PATH = "/usr/bin:/bin"
GIT_COMMAND_TIMEOUT_SECONDS = 60.0
MAX_GIT_STDOUT_BYTES = 32 * 1024 * 1024
MAX_GIT_PATH_BYTES = 64 * 1024
MAX_SCOPED_TREE_ENTRIES = 20_000
MAX_GIT_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_GIT_ARCHIVE_MEMBERS = 100_000
MAX_GIT_ARCHIVE_HEADERS = (2 * MAX_GIT_ARCHIVE_MEMBERS) + 1
MAX_GIT_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_GIT_ARCHIVE_PATH_BYTES = 4096
MAX_GIT_PAX_PAYLOAD_BYTES = MAX_GIT_ARCHIVE_PATH_BYTES + 256
MAX_TOTAL_PROTECTED_BLOB_BYTES = 64 * 1024 * 1024
RUNTIME_COMPONENT_DOMAIN = b"commerce-ops-desk:runtime-copy-input-component:v1\0"
RUNTIME_COMBINED_DOMAIN = b"commerce-ops-desk:runtime-copy-input-combined:v1\0"
RUNTIME_EXCLUDED_PREFIXES = (
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
RUNTIME_EXCLUDED_BASENAME_PATTERNS = (
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite3",
    "*.pyc",
)
RUNTIME_REQUIRED_ANCHORS = frozenset(
    {
        "backend/requirements.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "scripts/start-hosted.sh",
    }
)
RENAME_NOREPLACE = 1


class ProvenanceError(RuntimeError):
    """A safe provenance validation error without untrusted content."""


class ManifestPublishError(ProvenanceError):
    """Manifest publication failed before the evidence commit point."""

    committed = False

    def __init__(self, message: str, *, cleanup_complete: bool) -> None:
        super().__init__(message)
        self.cleanup_complete = cleanup_complete


class ManifestPublicationIndeterminate(ProvenanceError):
    """The evidence commit point may have passed; evidence must be retained."""

    committed = True


class _RenameNoreplaceError(OSError):
    """A native renameat2 failure known to be before the commit point."""


def _isolated_git_environment(temporary_directory: Path) -> dict[str, str]:
    """Create a minimal Git environment independent of ambient user state."""

    try:
        if (
            not isinstance(temporary_directory, Path)
            or not temporary_directory.is_absolute()
            or temporary_directory.resolve(strict=True) != temporary_directory
            or not temporary_directory.is_dir()
        ):
            raise ProvenanceError("isolated Git workspace is invalid")
        home = Path(tempfile.mkdtemp(prefix=".git-home-", dir=temporary_directory))
        xdg = Path(tempfile.mkdtemp(prefix=".git-xdg-", dir=temporary_directory))
        home.chmod(0o700)
        xdg.chmod(0o700)
    except ProvenanceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProvenanceError(
            "isolated Git environment could not be prepared"
        ) from None
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": SAFE_SYSTEM_PATH,
        "TMPDIR": str(temporary_directory),
        "XDG_CONFIG_HOME": str(xdg),
    }


def _run_git_bytes(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    maximum_stdout_bytes: int = MAX_GIT_STDOUT_BYTES,
) -> bytes:
    """Run one fixed Git command and return stdout without text conversion."""

    if (
        isinstance(maximum_stdout_bytes, bool)
        or not isinstance(maximum_stdout_bytes, int)
        or maximum_stdout_bytes < 0
    ):
        raise ProvenanceError("isolated Git output limit is invalid")
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [GIT_BINARY, "--no-replace-objects", *arguments],
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise ProvenanceError("isolated Git command failed")
        output = bytearray()
        deadline = time.monotonic() + GIT_COMMAND_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        [GIT_BINARY, "--no-replace-objects"],
                        GIT_COMMAND_TIMEOUT_SECONDS,
                    )
                events = selector.select(remaining)
                if not events:
                    raise subprocess.TimeoutExpired(
                        [GIT_BINARY, "--no-replace-objects"],
                        GIT_COMMAND_TIMEOUT_SECONDS,
                    )
                try:
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(64 * 1024, maximum_stdout_bytes + 1 - len(output)),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                output.extend(chunk)
                if len(output) > maximum_stdout_bytes:
                    raise ProvenanceError("isolated Git command output is too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(
                [GIT_BINARY, "--no-replace-objects"],
                GIT_COMMAND_TIMEOUT_SECONDS,
            )
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise ProvenanceError("isolated Git command failed")
        return bytes(output)
    except ProvenanceError:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    except (OSError, subprocess.TimeoutExpired):
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise ProvenanceError("isolated Git command failed") from None
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _assert_direct_git_commit(
    checkout: CanonicalCheckoutRoot,
    source_sha: str,
    *,
    temporary_directory: Path,
) -> None:
    """Bind one full SHA to a direct commit in the exact checkout."""

    if not isinstance(checkout, CanonicalCheckoutRoot):
        raise ProvenanceError("canonical checkout root is invalid")
    if (
        not isinstance(source_sha, str)
        or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
    ):
        raise ProvenanceError("Git revision identity is invalid")
    checkout.revalidate()
    environment = _isolated_git_environment(temporary_directory)
    common = ("-c", f"core.attributesFile={os.devnull}")
    toplevel = _run_git_bytes(
        (*common, "rev-parse", "--show-toplevel"),
        cwd=checkout.path,
        environment=environment,
    )
    if toplevel != os.fsencode(checkout.path) + b"\n":
        raise ProvenanceError(
            "Git repository top level must exactly match --repository"
        )
    object_type = _run_git_bytes(
        (*common, "cat-file", "-t", source_sha),
        cwd=checkout.path,
        environment=environment,
    )
    if object_type != b"commit\n":
        raise ProvenanceError("Git revision must name a commit object directly")
    checkout.revalidate()


def _assert_approved_git_revision(
    checkout: CanonicalCheckoutRoot,
    source_sha: str,
    approved_remote_ref: str,
    *,
    temporary_directory: Path,
) -> None:
    """Bind one direct commit to the exact checkout and approved remote ref."""

    if not _valid_remote_ref(approved_remote_ref):
        raise ProvenanceError("Git revision identity is invalid")
    _assert_direct_git_commit(
        checkout,
        source_sha,
        temporary_directory=temporary_directory,
    )
    environment = _isolated_git_environment(temporary_directory)
    common = ("-c", f"core.attributesFile={os.devnull}")
    if _run_git_bytes(
        (*common, "check-ref-format", approved_remote_ref),
        cwd=checkout.path,
        environment=environment,
    ):
        raise ProvenanceError("approved remote ref is invalid")
    approved_sha = _run_git_bytes(
        (*common, "show-ref", "--verify", "--hash", approved_remote_ref),
        cwd=checkout.path,
        environment=environment,
    )
    if approved_sha != source_sha.encode("ascii") + b"\n":
        raise ProvenanceError("Git revision does not match the approved remote ref")
    checkout.revalidate()


def _prepare_isolated_object_view(
    checkout: CanonicalCheckoutRoot,
    *,
    temporary_directory: Path,
    environment: Mapping[str, str],
) -> Path:
    """Create a bare view whose only object source is the validated repository."""

    if not isinstance(checkout, CanonicalCheckoutRoot):
        raise ProvenanceError("canonical checkout root is invalid")
    checkout.revalidate()
    raw_object_directory = _run_git_bytes(
        (
            "-c",
            f"core.attributesFile={os.devnull}",
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ),
        cwd=checkout.path,
        environment=environment,
        maximum_stdout_bytes=MAX_GIT_PATH_BYTES + 1,
    )
    if (
        not raw_object_directory.endswith(b"\n")
        or len(raw_object_directory) <= 1
        or len(raw_object_directory) > MAX_GIT_PATH_BYTES + 1
    ):
        raise ProvenanceError("repository object database is invalid")
    encoded_path = raw_object_directory[:-1]
    if any(character in encoded_path for character in (b"\x00", b"\r", b"\n")):
        raise ProvenanceError("repository object database is invalid")
    try:
        object_directory = Path(os.fsdecode(encoded_path))
        if (
            not object_directory.is_absolute()
            or object_directory.resolve(strict=True) != object_directory
            or not object_directory.is_dir()
        ):
            raise ProvenanceError("repository object database is invalid")
    except ProvenanceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProvenanceError("repository object database is invalid") from None

    object_view: Path | None = None
    try:
        object_view = Path(
            tempfile.mkdtemp(
                prefix=".source-object-view-",
                dir=temporary_directory,
            )
        )
        _run_git_bytes(
            (
                "-c",
                "init.templateDir=",
                "init",
                "--bare",
                str(object_view),
            ),
            cwd=temporary_directory,
            environment=environment,
        )
        alternates = object_view / "objects" / "info" / "alternates"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(alternates, flags, 0o600)
        try:
            _write_all(descriptor, encoded_path + b"\n")
        finally:
            os.close(descriptor)
        checkout.revalidate()
        return object_view
    except ProvenanceError:
        if object_view is not None:
            shutil.rmtree(object_view, ignore_errors=True)
        raise
    except (OSError, RuntimeError, ValueError):
        if object_view is not None:
            shutil.rmtree(object_view, ignore_errors=True)
        raise ProvenanceError(
            "isolated Git object view could not be prepared"
        ) from None


def _is_canonical_repository_path(path: object) -> bool:
    """Return whether a Git/tar path is unambiguous under the release policy."""

    if not isinstance(path, str) or not path:
        return False
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    components = path.split("/")
    return (
        len(encoded) <= MAX_GIT_ARCHIVE_PATH_BYTES
        and not path.startswith("/")
        and "\\" not in path
        and all(component and component not in {".", ".."} for component in components)
        and all(
            unicodedata.category(character) not in {"Cc", "Cf"} for character in path
        )
    )


def _parse_scoped_tree(raw: bytes) -> Mapping[str, TreeEntry]:
    """Parse the exact NUL-delimited tree projection used by runtime policy."""

    if type(raw) is not bytes or not raw or not raw.endswith(b"\0"):
        raise ProvenanceError("Git tree output is invalid")
    if raw.count(b"\0") > MAX_SCOPED_TREE_ENTRIES:
        raise ProvenanceError("Git tree output is too large")
    entries: dict[str, TreeEntry] = {}
    for record_number, record in enumerate(raw[:-1].split(b"\0"), start=1):
        if record_number > MAX_SCOPED_TREE_ENTRIES:
            raise ProvenanceError("Git tree output is too large")
        if not record:
            raise ProvenanceError("Git tree output is invalid")
        header, separator, encoded_path = record.partition(b"\t")
        if not separator or not encoded_path or b"\t" in encoded_path:
            raise ProvenanceError("Git tree output is invalid")
        fields = header.split(b" ")
        if len(fields) != 3 or any(not field for field in fields):
            raise ProvenanceError("Git tree output is invalid")
        encoded_mode, encoded_type, encoded_object_id = fields
        if re.fullmatch(rb"[0-9a-f]{40}", encoded_object_id) is None:
            raise ProvenanceError("Git tree output is invalid")
        try:
            path = encoded_path.decode("utf-8", errors="strict")
            mode = encoded_mode.decode("ascii", errors="strict")
            object_type = encoded_type.decode("ascii", errors="strict")
            object_id = encoded_object_id.decode("ascii")
        except UnicodeDecodeError:
            raise ProvenanceError("Git tree output is invalid") from None
        if not _is_canonical_repository_path(path) or path in entries:
            raise ProvenanceError("Git tree output is invalid")

        root_file = path in {".dockerignore", "Dockerfile"}
        runtime_root = path in {"backend", "frontend", "scripts"}
        runtime_descendant = any(
            path.startswith(f"{root}/") for root in ("backend", "frontend", "scripts")
        )
        if root_file:
            valid_shape = mode in {"100644", "100755"} and object_type == "blob"
        elif runtime_root:
            valid_shape = mode == "040000" and object_type == "tree"
        elif runtime_descendant:
            valid_shape = (mode, object_type) in {
                ("040000", "tree"),
                ("100644", "blob"),
                ("100755", "blob"),
            }
        else:
            valid_shape = False
        if not valid_shape:
            raise ProvenanceError("Git tree output is invalid")
        entries[path] = TreeEntry(
            mode=mode,
            object_type=object_type,
            object_id=object_id,
            path=path,
        )
    return MappingProxyType(entries)


def _parse_tar_octal(field: bytes) -> int:
    """Parse one traditional positive tar numeric field fail-closed."""

    if not field or field[0] & 0x80:
        raise ProvenanceError("Git archive header is invalid")
    stripped = field.rstrip(b"\0 ").lstrip(b" ")
    if not stripped:
        return 0
    if any(character < ord("0") or character > ord("7") for character in stripped):
        raise ProvenanceError("Git archive header is invalid")
    return int(stripped, 8)


def _validate_tar_header_checksum(header: bytes) -> None:
    """Require the standard unsigned tar checksum for one complete header."""

    if len(header) != tarfile.BLOCKSIZE:
        raise ProvenanceError("Git archive header is invalid")
    stored = _parse_tar_octal(header[148:156])
    observed = sum(header[:148]) + (8 * ord(" ")) + sum(header[156:])
    if stored != observed:
        raise ProvenanceError("Git archive header checksum is invalid")


def _parse_pax_records(payload: bytes) -> Mapping[str, str]:
    """Parse a complete, duplicate-free POSIX PAX payload."""

    if not payload or len(payload) > MAX_GIT_PAX_PAYLOAD_BYTES:
        raise ProvenanceError("Git archive PAX metadata is invalid")
    records: dict[str, str] = {}
    offset = 0
    while offset < len(payload):
        separator = payload.find(b" ", offset)
        if separator <= offset:
            raise ProvenanceError("Git archive PAX metadata is invalid")
        encoded_length = payload[offset:separator]
        if (
            not encoded_length.isdigit()
            or encoded_length.startswith(b"0")
            or len(encoded_length) > 10
        ):
            raise ProvenanceError("Git archive PAX metadata is invalid")
        record_length = int(encoded_length)
        record_end = offset + record_length
        if record_end > len(payload) or payload[record_end - 1 : record_end] != b"\n":
            raise ProvenanceError("Git archive PAX metadata is invalid")
        record = payload[separator + 1 : record_end - 1]
        encoded_key, equals, encoded_value = record.partition(b"=")
        if (
            not equals
            or not encoded_key
            or b"\0" in encoded_value
            or b"\r" in encoded_value
        ):
            raise ProvenanceError("Git archive PAX metadata is invalid")
        try:
            key = encoded_key.decode("ascii", errors="strict")
            value = encoded_value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ProvenanceError("Git archive PAX metadata is invalid") from None
        if key in records or record_length != len(payload[offset:record_end]):
            raise ProvenanceError("Git archive PAX metadata is invalid")
        records[key] = value
        offset = record_end
    if offset != len(payload):
        raise ProvenanceError("Git archive PAX metadata is invalid")
    return MappingProxyType(records)


def _decode_tar_header_path(header: bytes) -> str:
    """Decode one USTAR name/prefix pair without surrogate recovery."""

    encoded_name = header[:100].split(b"\0", maxsplit=1)[0]
    encoded_prefix = header[345:500].split(b"\0", maxsplit=1)[0]
    if not encoded_name:
        raise ProvenanceError("Git archive header path is invalid")
    encoded_path = (
        encoded_prefix + b"/" + encoded_name if encoded_prefix else encoded_name
    )
    try:
        return encoded_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProvenanceError("Git archive header path is invalid") from None


def _validate_raw_git_archive(
    raw: bytes,
    *,
    source_sha: str,
) -> tuple[_RawTarMember, ...]:
    """Strictly scan raw Git tar headers before the high-level parser."""

    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_GIT_ARCHIVE_BYTES
        or len(raw) % tarfile.BLOCKSIZE != 0
    ):
        raise ProvenanceError("Git archive envelope is invalid")
    zero_block = b"\0" * tarfile.BLOCKSIZE
    offset = 0
    raw_header_count = 0
    semantic_members: list[_RawTarMember] = []
    seen_paths: set[str] = set()
    saw_global_header = False
    pending_pax_path: str | None = None
    terminated = False
    while offset < len(raw):
        header = raw[offset : offset + tarfile.BLOCKSIZE]
        if header == zero_block:
            if (
                pending_pax_path is not None
                or offset + (2 * tarfile.BLOCKSIZE) > len(raw)
                or raw[offset + tarfile.BLOCKSIZE : offset + (2 * tarfile.BLOCKSIZE)]
                != zero_block
                or any(raw[offset + (2 * tarfile.BLOCKSIZE) :])
            ):
                raise ProvenanceError("Git archive termination is invalid")
            terminated = True
            break
        raw_header_count += 1
        if raw_header_count > MAX_GIT_ARCHIVE_HEADERS:
            raise ProvenanceError("Git archive has too many headers")
        _validate_tar_header_checksum(header)
        if header[257:263] != b"ustar\0" or header[263:265] != b"00":
            raise ProvenanceError("Git archive header format is invalid")
        mode = _parse_tar_octal(header[100:108])
        _parse_tar_octal(header[108:116])
        _parse_tar_octal(header[116:124])
        size = _parse_tar_octal(header[124:136])
        _parse_tar_octal(header[136:148])
        _parse_tar_octal(header[329:337])
        _parse_tar_octal(header[337:345])
        type_flag = header[156:157]
        data_start = offset + tarfile.BLOCKSIZE
        data_end = data_start + size
        padded_end = data_start + (
            ((size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
        )
        if padded_end > len(raw) or any(raw[data_end:padded_end]):
            raise ProvenanceError("Git archive member padding is invalid")
        payload = raw[data_start:data_end]

        if type_flag == b"g":
            if (
                saw_global_header
                or raw_header_count != 1
                or pending_pax_path is not None
            ):
                raise ProvenanceError("Git archive global metadata is invalid")
            if dict(_parse_pax_records(payload)) != {"comment": source_sha}:
                raise ProvenanceError("Git archive global metadata is invalid")
            saw_global_header = True
        elif type_flag == b"x":
            if not saw_global_header or pending_pax_path is not None:
                raise ProvenanceError("Git archive member metadata is invalid")
            records = _parse_pax_records(payload)
            pax_path = records.get("path")
            if (
                set(records) != {"path"}
                or not isinstance(pax_path, str)
                or not (
                    _is_canonical_repository_path(pax_path)
                    or (
                        pax_path.endswith("/")
                        and _is_canonical_repository_path(pax_path[:-1])
                    )
                )
            ):
                raise ProvenanceError("Git archive member metadata is invalid")
            pending_pax_path = pax_path
        elif type_flag in {b"0", b"5"}:
            if not saw_global_header:
                raise ProvenanceError("Git archive global metadata is missing")
            header_path = _decode_tar_header_path(header)
            pax_path = pending_pax_path
            path = pax_path or header_path
            pending_pax_path = None
            if type_flag == b"5" and path.endswith("/"):
                path = path[:-1]
            if (
                not _is_canonical_repository_path(path)
                or path in seen_paths
                or mode & ~0o777
                or size > MAX_GIT_ARCHIVE_MEMBER_BYTES
                or (type_flag == b"5" and size != 0)
            ):
                raise ProvenanceError("Git archive member header is invalid")
            seen_paths.add(path)
            semantic_members.append(
                _RawTarMember(
                    path=path,
                    type_flag=type_flag,
                    mode=mode,
                    size=size,
                    pax_path=pax_path,
                )
            )
            if len(semantic_members) > MAX_GIT_ARCHIVE_MEMBERS:
                raise ProvenanceError("Git archive has too many members")
        else:
            raise ProvenanceError("Git archive member type is invalid")
        offset = padded_end
    if not terminated or not saw_global_header or not semantic_members:
        raise ProvenanceError("Git archive envelope is invalid")
    return tuple(semantic_members)


def _validate_git_archive(
    raw: bytes,
    *,
    source_sha: str,
    tree_entries: Mapping[str, TreeEntry],
    blob_bytes: Mapping[str, bytes],
) -> Mapping[str, ArchiveFile]:
    """Bind protected archive members to the scoped tree and raw blobs."""

    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_GIT_ARCHIVE_BYTES
        or not isinstance(source_sha, str)
        or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
        or not isinstance(tree_entries, Mapping)
        or not isinstance(blob_bytes, Mapping)
    ):
        raise ProvenanceError("Git archive is invalid")
    expected_blob_paths = {
        path for path, entry in tree_entries.items() if entry.object_type == "blob"
    }
    exported_asset_paths = expected_blob_paths & DEPLOYMENT_ASSET_PATHS
    expected_raw_blob_paths = expected_blob_paths - exported_asset_paths
    if set(blob_bytes) != expected_blob_paths or any(
        type(content) is not bytes for content in blob_bytes.values()
    ):
        raise ProvenanceError("Git archive evidence is invalid")
    raw_members = _validate_raw_git_archive(raw, source_sha=source_sha)

    seen: set[str] = set()
    seen_scoped: set[str] = set()
    files: dict[str, ArchiveFile] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            if archive.pax_headers != {"comment": source_sha}:
                raise ProvenanceError("Git archive metadata is invalid")
            for member_number, member in enumerate(archive, start=1):
                if member_number > MAX_GIT_ARCHIVE_MEMBERS:
                    raise ProvenanceError("Git archive is too large")
                if member_number > len(raw_members):
                    raise ProvenanceError("Git archive parser disagreement")
                raw_member = raw_members[member_number - 1]
                expected_pax_headers = {"comment": source_sha}
                if raw_member.pax_path is not None:
                    expected_pax_headers["path"] = raw_member.pax_path
                if member.pax_headers != expected_pax_headers:
                    raise ProvenanceError("Git archive metadata is invalid")
                path = member.name
                if (
                    not _is_canonical_repository_path(path)
                    or path in seen
                    or member.mode & ~0o777
                    or not (member.isreg() or member.isdir())
                    or raw_member.path != path
                    or raw_member.mode != member.mode
                    or raw_member.size != member.size
                    or (raw_member.type_flag == b"5") != member.isdir()
                ):
                    raise ProvenanceError("Git archive member is invalid")
                seen.add(path)

                entry = tree_entries.get(path)
                if entry is None:
                    if path in {".dockerignore", "Dockerfile"} or any(
                        path == root or path.startswith(f"{root}/")
                        for root in ("backend", "frontend", "scripts")
                    ):
                        raise ProvenanceError("Git archive tree binding is invalid")
                    continue
                seen_scoped.add(path)
                if entry.object_type == "tree":
                    if not member.isdir() or member.mode != 0o775:
                        raise ProvenanceError("Git archive tree binding is invalid")
                    continue
                expected_mode = "100755" if member.mode & 0o111 else "100644"
                expected_tar_mode = 0o775 if expected_mode == "100755" else 0o664
                if (
                    not member.isreg()
                    or member.mode != expected_tar_mode
                    or entry.mode != expected_mode
                    or member.size < 0
                    or member.size > MAX_GIT_ARCHIVE_MEMBER_BYTES
                ):
                    raise ProvenanceError("Git archive tree binding is invalid")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ProvenanceError("Git archive member is invalid")
                content = extracted.read(MAX_GIT_ARCHIVE_MEMBER_BYTES + 1)
                if (
                    len(content) != member.size
                    or len(content) > MAX_GIT_ARCHIVE_MEMBER_BYTES
                    or (path in expected_raw_blob_paths and content != blob_bytes[path])
                ):
                    raise ProvenanceError("Git archive blob binding is invalid")
                files[path] = ArchiveFile(
                    path=path,
                    mode=entry.mode,
                    data=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            if len(seen) != len(raw_members):
                raise ProvenanceError("Git archive parser disagreement")
    except ProvenanceError:
        raise
    except (OSError, tarfile.TarError, ValueError):
        raise ProvenanceError("Git archive is invalid") from None
    if seen_scoped != set(tree_entries):
        raise ProvenanceError("Git archive tree binding is incomplete")
    return MappingProxyType(files)


def _canonical_runtime_component(
    component: str,
    entries: tuple[RuntimeInventoryEntry, ...],
) -> CanonicalDigest:
    """Serialize and hash one runtime-copy component inventory."""

    if component not in {"backend", "frontend", "scripts"} or not isinstance(
        entries, tuple
    ):
        raise ProvenanceError("runtime component inventory is invalid")
    seen: set[str] = set()
    validated: list[RuntimeInventoryEntry] = []
    for entry in entries:
        if (
            not isinstance(entry, RuntimeInventoryEntry)
            or not entry.path.startswith(f"{component}/")
            or entry.path in seen
            or entry.mode not in {"100644", "100755"}
            or SHA256_PATTERN.fullmatch(entry.sha256) is None
        ):
            raise ProvenanceError("runtime component inventory is invalid")
        try:
            entry.path.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ProvenanceError("runtime component inventory is invalid") from None
        seen.add(entry.path)
        validated.append(entry)
    validated.sort(key=lambda entry: entry.path.encode("utf-8"))
    canonical = canonical_json_bytes(
        {
            "component": component,
            "entries": [
                {
                    "mode": entry.mode,
                    "path": entry.path,
                    "sha256": entry.sha256,
                }
                for entry in validated
            ],
            "schema": 1,
        }
    )
    return CanonicalDigest(
        canonical_bytes=canonical,
        sha256=length_framed_sha256(RUNTIME_COMPONENT_DOMAIN, canonical),
    )


def _canonical_dockerignore_component(content: bytes) -> CanonicalDigest:
    """Serialize and hash the raw .dockerignore component."""

    if type(content) is not bytes:
        raise ProvenanceError("dockerignore inventory is invalid")
    canonical = canonical_json_bytes(
        {
            "component": "dockerignore",
            "schema": 1,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    return CanonicalDigest(
        canonical_bytes=canonical,
        sha256=length_framed_sha256(RUNTIME_COMPONENT_DOMAIN, canonical),
    )


def _canonical_combined_runtime_inventory(
    component_sha256: Mapping[str, str],
) -> CanonicalDigest:
    """Serialize and hash the exact four-component runtime projection."""

    if (
        not isinstance(component_sha256, Mapping)
        or set(component_sha256) != RUNTIME_COMPONENTS
        or any(
            not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
            for digest in component_sha256.values()
        )
    ):
        raise ProvenanceError("combined runtime inventory is invalid")
    canonical = canonical_json_bytes(
        {
            "components": dict(component_sha256),
            "policy": RUNTIME_COPY_INPUTS_POLICY,
            "schema": 1,
        }
    )
    return CanonicalDigest(
        canonical_bytes=canonical,
        sha256=length_framed_sha256(RUNTIME_COMBINED_DOMAIN, canonical),
    )


def _build_runtime_inventory(files: Mapping[str, ArchiveFile]) -> RuntimeInventory:
    """Project validated archive files through runtime-copy-inputs-v1."""

    if not isinstance(files, Mapping):
        raise ProvenanceError("runtime inventory input is invalid")
    for path, archived in files.items():
        if (
            not isinstance(path, str)
            or not isinstance(archived, ArchiveFile)
            or archived.path != path
            or archived.mode not in {"100644", "100755"}
            or archived.sha256 != hashlib.sha256(archived.data).hexdigest()
        ):
            raise ProvenanceError("runtime inventory input is invalid")
    dockerignore = files.get(".dockerignore")
    if not isinstance(dockerignore, ArchiveFile):
        raise ProvenanceError("runtime inventory anchor is missing")

    by_component: dict[str, list[RuntimeInventoryEntry]] = {
        "backend": [],
        "frontend": [],
        "scripts": [],
    }
    retained_paths: set[str] = set()
    for path, archived in files.items():
        component = path.partition("/")[0]
        if component not in by_component or "/" not in path:
            continue
        if any(path.startswith(prefix) for prefix in RUNTIME_EXCLUDED_PREFIXES):
            continue
        basename = path.rsplit("/", maxsplit=1)[-1]
        if any(
            fnmatchcase(basename, pattern)
            for pattern in RUNTIME_EXCLUDED_BASENAME_PATTERNS
        ):
            continue
        retained_paths.add(path)
        by_component[component].append(
            RuntimeInventoryEntry(
                path=path,
                mode=archived.mode,
                sha256=archived.sha256,
            )
        )
    if not RUNTIME_REQUIRED_ANCHORS <= retained_paths:
        raise ProvenanceError("runtime inventory anchor is missing")

    component_evidence: dict[str, CanonicalDigest] = {
        component: _canonical_runtime_component(component, tuple(entries))
        for component, entries in by_component.items()
    }
    component_evidence["dockerignore"] = _canonical_dockerignore_component(
        dockerignore.data
    )
    immutable_components = MappingProxyType(component_evidence)
    combined = _canonical_combined_runtime_inventory(
        {name: evidence.sha256 for name, evidence in component_evidence.items()}
    )
    return RuntimeInventory(
        components=immutable_components,
        combined=combined,
    )


def _parse_fixed_blob_tree(raw: bytes, expected_path: str) -> TreeEntry:
    """Parse one exact ls-tree blob record for a fixed repository path."""

    encoded_path = expected_path.encode("utf-8")
    if type(raw) is not bytes or not raw.endswith(b"\0") or raw.count(b"\0") != 1:
        raise ProvenanceError("Git blob identity is invalid")
    header, separator, observed_path = raw[:-1].partition(b"\t")
    fields = header.split(b" ")
    if (
        not separator
        or observed_path != encoded_path
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
        or re.fullmatch(rb"[0-9a-f]{40}", fields[2]) is None
    ):
        raise ProvenanceError("Git blob identity is invalid")
    return TreeEntry(
        mode=fields[0].decode("ascii"),
        object_type="blob",
        object_id=fields[2].decode("ascii"),
        path=expected_path,
    )


def _parse_optional_fixed_blob_tree(
    raw: bytes,
    expected_path: str,
) -> TreeEntry | None:
    """Parse an optional exact blob whose absence is represented by no output."""

    if raw == b"":
        return None
    return _parse_fixed_blob_tree(raw, expected_path)


def _extract_validated_git_archive(raw: bytes, destination: Path) -> None:
    """Extract a previously validated regular-file/directory Git archive."""

    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ProvenanceError("Git archive destination is invalid")
    created_identity: tuple[int, int] | None = None
    completed = False
    try:
        destination.mkdir(mode=0o700)
        metadata = destination.lstat()
        created_identity = (metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or destination.resolve(strict=True) != destination
        ):
            raise ProvenanceError("Git archive destination is invalid")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            archive.extractall(destination, filter="data")
        completed = True
    except ProvenanceError:
        raise
    except (OSError, tarfile.TarError, tarfile.FilterError, ValueError):
        raise ProvenanceError("Git archive could not be extracted safely") from None
    finally:
        if not completed and created_identity is not None:
            try:
                current = destination.lstat()
            except OSError:
                pass
            else:
                if (
                    stat.S_ISDIR(current.st_mode)
                    and not stat.S_ISLNK(current.st_mode)
                    and (current.st_dev, current.st_ino) == created_identity
                ):
                    shutil.rmtree(destination, ignore_errors=True)


def _collect_blob_bytes(
    entries: Mapping[str, TreeEntry],
    *,
    git_dir: str,
    workspace: Path,
    environment: Mapping[str, str],
) -> Mapping[str, bytes]:
    """Read protected blobs with both per-object and cumulative bounds."""

    if not isinstance(entries, Mapping):
        raise ProvenanceError("Git blob evidence is invalid")
    blobs: dict[str, bytes] = {}
    total_bytes = 0
    for path, entry in entries.items():
        if not isinstance(entry, TreeEntry) or entry.path != path:
            raise ProvenanceError("Git blob evidence is invalid")
        if entry.object_type != "blob":
            continue
        remaining_bytes = MAX_TOTAL_PROTECTED_BLOB_BYTES - total_bytes
        if remaining_bytes < 0:
            raise ProvenanceError("Git blob evidence is too large")
        content = _run_git_bytes(
            (git_dir, "cat-file", "blob", entry.object_id),
            cwd=workspace,
            environment=environment,
            maximum_stdout_bytes=min(
                MAX_GIT_ARCHIVE_MEMBER_BYTES,
                remaining_bytes,
            ),
        )
        git_header = f"blob {len(content)}\0".encode("ascii")
        observed_object_id = hashlib.sha1(
            git_header + content,
            usedforsecurity=False,
        ).hexdigest()
        if observed_object_id != entry.object_id:
            raise ProvenanceError("Git blob object identity is invalid")
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_PROTECTED_BLOB_BYTES:
            raise ProvenanceError("Git blob evidence is too large")
        blobs[path] = content
    return MappingProxyType(blobs)


def _export_revision_evidence_from_commit(
    checkout: CanonicalCheckoutRoot,
    source_sha: str,
    *,
    workspace: Path,
) -> _RevisionExport:
    """Export and bind one direct commit without reading worktree files."""

    if not isinstance(checkout, CanonicalCheckoutRoot):
        raise ProvenanceError("canonical checkout root is invalid")
    _assert_direct_git_commit(
        checkout,
        source_sha,
        temporary_directory=workspace,
    )
    environment = _isolated_git_environment(workspace)
    object_view = _prepare_isolated_object_view(
        checkout,
        temporary_directory=workspace,
        environment=environment,
    )
    git_dir = f"--git-dir={object_view}"
    tree_raw = _run_git_bytes(
        (
            git_dir,
            "ls-tree",
            "-rz",
            "-t",
            "--full-tree",
            source_sha,
            "--",
            "backend",
            "frontend",
            "scripts",
            ".dockerignore",
            "Dockerfile",
        ),
        cwd=workspace,
        environment=environment,
    )
    scoped_entries = _parse_scoped_tree(tree_raw)
    all_entries = dict(scoped_entries)
    for asset_path in sorted(DEPLOYMENT_ASSET_PATHS, key=lambda value: value.encode()):
        asset_raw = _run_git_bytes(
            (
                git_dir,
                "ls-tree",
                "-z",
                "--full-tree",
                source_sha,
                "--",
                asset_path,
            ),
            cwd=workspace,
            environment=environment,
            maximum_stdout_bytes=MAX_GIT_PATH_BYTES,
        )
        asset_entry = _parse_fixed_blob_tree(asset_raw, asset_path)
        if asset_path in all_entries:
            raise ProvenanceError("Git tree output is invalid")
        all_entries[asset_path] = asset_entry

    blob_identity_entries = dict(all_entries)
    for attributes_path in sorted(
        ARCHIVE_ATTRIBUTE_PATHS,
        key=lambda value: value.encode(),
    ):
        attributes_raw = _run_git_bytes(
            (
                git_dir,
                "ls-tree",
                "-z",
                "--full-tree",
                source_sha,
                "--",
                attributes_path,
            ),
            cwd=workspace,
            environment=environment,
            maximum_stdout_bytes=MAX_GIT_PATH_BYTES,
        )
        attributes_entry = _parse_optional_fixed_blob_tree(
            attributes_raw,
            attributes_path,
        )
        if attributes_entry is None:
            continue
        if attributes_path in blob_identity_entries:
            raise ProvenanceError("Git tree output is invalid")
        blob_identity_entries[attributes_path] = attributes_entry

    blobs = _collect_blob_bytes(
        MappingProxyType(blob_identity_entries),
        git_dir=git_dir,
        workspace=workspace,
        environment=environment,
    )
    archive_raw = _run_git_bytes(
        (
            git_dir,
            "-c",
            f"core.attributesFile={os.devnull}",
            "archive",
            "--format=tar",
            source_sha,
        ),
        cwd=workspace,
        environment=environment,
        maximum_stdout_bytes=MAX_GIT_ARCHIVE_BYTES,
    )
    files = _validate_git_archive(
        archive_raw,
        source_sha=source_sha,
        tree_entries=MappingProxyType(all_entries),
        blob_bytes=MappingProxyType(
            {
                path: blobs[path]
                for path, entry in all_entries.items()
                if entry.object_type == "blob"
            }
        ),
    )
    runtime_inventory = _build_runtime_inventory(files)
    dockerfile = files.get("Dockerfile")
    dockerfile_entry = all_entries.get("Dockerfile")
    if not isinstance(dockerfile, ArchiveFile) or not isinstance(
        dockerfile_entry, TreeEntry
    ):
        raise ProvenanceError("Dockerfile evidence is invalid")
    deployment_digests = {path: files[path].sha256 for path in DEPLOYMENT_ASSET_PATHS}
    _assert_direct_git_commit(
        checkout,
        source_sha,
        temporary_directory=workspace,
    )
    return _RevisionExport(
        evidence=RevisionEvidence(
            source_sha=source_sha,
            deployment_assets=DeploymentAssets(
                schema=1,
                sha256=MappingProxyType(deployment_digests),
            ),
            runtime_inventory=runtime_inventory,
            dockerfile_mode=dockerfile.mode,
            dockerfile_blob_sha=dockerfile_entry.object_id,
            dockerfile_sha256=dockerfile.sha256,
        ),
        archive=archive_raw,
    )


def export_revision_evidence(
    checkout: CanonicalCheckoutRoot,
    source_sha: str,
    approved_remote_ref: str,
    *,
    workspace: Path,
    extract_to: Path | None = None,
) -> RevisionEvidence:
    """Export and bind one approved revision without reading worktree files."""

    try:
        if (
            not isinstance(workspace, Path)
            or not workspace.is_absolute()
            or workspace.resolve(strict=True) != workspace
            or not workspace.is_dir()
        ):
            raise ProvenanceError("Git evidence workspace is invalid")
        with tempfile.TemporaryDirectory(
            prefix=".approved-revision-",
            dir=workspace,
        ) as temporary:
            operation_workspace = Path(temporary)
            _assert_approved_git_revision(
                checkout,
                source_sha,
                approved_remote_ref,
                temporary_directory=operation_workspace,
            )
            exported = _export_revision_evidence_from_commit(
                checkout,
                source_sha,
                workspace=operation_workspace,
            )
            _assert_approved_git_revision(
                checkout,
                source_sha,
                approved_remote_ref,
                temporary_directory=operation_workspace,
            )
        if extract_to is not None:
            _extract_validated_git_archive(exported.archive, extract_to)
        return exported.evidence
    except ProvenanceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProvenanceError("Git evidence workspace could not be used") from None


def _git_is_ancestor(
    checkout: CanonicalCheckoutRoot,
    ancestor_sha: str,
    descendant_sha: str,
    *,
    workspace: Path,
) -> bool:
    """Return Git's exact ancestry result under the isolated environment."""

    if (
        not isinstance(checkout, CanonicalCheckoutRoot)
        or SOURCE_SHA_PATTERN.fullmatch(ancestor_sha) is None
        or SOURCE_SHA_PATTERN.fullmatch(descendant_sha) is None
    ):
        raise ProvenanceError("Git ancestry identity is invalid")
    checkout.revalidate()
    environment = _isolated_git_environment(workspace)
    object_view = _prepare_isolated_object_view(
        checkout,
        temporary_directory=workspace,
        environment=environment,
    )
    try:
        result = subprocess.run(
            [
                GIT_BINARY,
                "--no-replace-objects",
                f"--git-dir={object_view}",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "core.commitGraph=false",
                "merge-base",
                "--is-ancestor",
                ancestor_sha,
                descendant_sha,
            ],
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProvenanceError("Git ancestry could not be verified") from None
    if result.returncode not in {0, 1}:
        raise ProvenanceError("Git ancestry could not be verified")
    checkout.revalidate()
    return result.returncode == 0


def _runtime_equivalence_from_revisions(
    donor: RevisionEvidence,
    target: RevisionEvidence,
) -> RuntimeEquivalence:
    """Require identical runtime inputs and the single reviewed recipe change."""

    if (
        not isinstance(donor, RevisionEvidence)
        or not isinstance(target, RevisionEvidence)
        or donor.source_sha == target.source_sha
    ):
        raise ProvenanceError("runtime equivalence evidence is invalid")
    for inventory in (donor.runtime_inventory, target.runtime_inventory):
        if (
            not isinstance(inventory, RuntimeInventory)
            or not isinstance(inventory.components, Mapping)
            or set(inventory.components) != RUNTIME_COMPONENTS
            or not isinstance(inventory.combined, CanonicalDigest)
        ):
            raise ProvenanceError("runtime equivalence evidence is invalid")
        for component in RUNTIME_COMPONENTS:
            evidence = inventory.components[component]
            if not isinstance(
                evidence, CanonicalDigest
            ) or evidence.sha256 != length_framed_sha256(
                RUNTIME_COMPONENT_DOMAIN,
                evidence.canonical_bytes,
            ):
                raise ProvenanceError("runtime equivalence evidence is invalid")
        expected_combined = _canonical_combined_runtime_inventory(
            {
                component: inventory.components[component].sha256
                for component in RUNTIME_COMPONENTS
            }
        )
        if inventory.combined != expected_combined:
            raise ProvenanceError("runtime equivalence evidence is invalid")
    component_pairs: dict[str, DigestPair] = {}
    for component in sorted(RUNTIME_COMPONENTS):
        donor_digest = donor.runtime_inventory.components[component].sha256
        target_digest = target.runtime_inventory.components[component].sha256
        if donor_digest != target_digest:
            raise ProvenanceError("runtime copy inputs are not equivalent")
        component_pairs[component] = DigestPair(
            donor_sha256=donor_digest,
            target_sha256=target_digest,
        )
    donor_combined = donor.runtime_inventory.combined.sha256
    target_combined = target.runtime_inventory.combined.sha256
    if donor_combined != target_combined:
        raise ProvenanceError("runtime copy inputs are not equivalent")
    if (
        donor.dockerfile_mode != "100644"
        or target.dockerfile_mode != "100644"
        or donor.dockerfile_blob_sha != DONOR_DOCKERFILE_BLOB_SHA
        or target.dockerfile_blob_sha != TARGET_DOCKERFILE_BLOB_SHA
        or donor.dockerfile_sha256 != DONOR_DOCKERFILE_SHA256
        or target.dockerfile_sha256 != TARGET_DOCKERFILE_SHA256
    ):
        raise ProvenanceError("Dockerfile transition is not approved")
    return RuntimeEquivalence(
        schema=1,
        policy=RUNTIME_EQUIVALENCE_POLICY,
        donor_copy_inputs_sha256=donor_combined,
        target_copy_inputs_sha256=target_combined,
        components=MappingProxyType(component_pairs),
        dockerfile_transition=DockerfileTransition(
            policy=DOCKERFILE_TRANSITION_POLICY,
            from_sha256=DONOR_DOCKERFILE_SHA256,
            to_sha256=TARGET_DOCKERFILE_SHA256,
        ),
    )


def recompute_runtime_equivalence(
    checkout: CanonicalCheckoutRoot,
    *,
    donor_sha: str,
    target_sha: str,
    approved_remote_ref: str,
    workspace: Path,
) -> ReuseGitEvidence:
    """Recompute the complete one-hop Git and runtime equivalence proof."""

    try:
        if (
            not isinstance(workspace, Path)
            or not workspace.is_absolute()
            or workspace.resolve(strict=True) != workspace
            or not workspace.is_dir()
        ):
            raise ProvenanceError("Git evidence workspace is invalid")
        with tempfile.TemporaryDirectory(
            prefix=".runtime-equivalence-",
            dir=workspace,
        ) as temporary:
            operation_workspace = Path(temporary)
            return _recompute_runtime_equivalence_in_workspace(
                checkout,
                donor_sha=donor_sha,
                target_sha=target_sha,
                approved_remote_ref=approved_remote_ref,
                workspace=operation_workspace,
            )
    except ProvenanceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProvenanceError("Git evidence workspace could not be used") from None


def _recompute_runtime_equivalence_in_workspace(
    checkout: CanonicalCheckoutRoot,
    *,
    donor_sha: str,
    target_sha: str,
    approved_remote_ref: str,
    workspace: Path,
) -> ReuseGitEvidence:
    """Recompute equivalence inside one caller-owned ephemeral workspace."""

    _assert_direct_git_commit(checkout, donor_sha, temporary_directory=workspace)
    _assert_approved_git_revision(
        checkout,
        target_sha,
        approved_remote_ref,
        temporary_directory=workspace,
    )
    _assert_direct_git_commit(
        checkout,
        OBSERVED_BASELINE_SHA,
        temporary_directory=workspace,
    )
    if (
        donor_sha == target_sha
        or target_sha == OBSERVED_BASELINE_SHA
        or not _git_is_ancestor(
            checkout,
            donor_sha,
            target_sha,
            workspace=workspace,
        )
        or not _git_is_ancestor(
            checkout,
            OBSERVED_BASELINE_SHA,
            target_sha,
            workspace=workspace,
        )
    ):
        raise ProvenanceError("Git ancestry is not approved for runtime reuse")
    donor_export = _export_revision_evidence_from_commit(
        checkout,
        donor_sha,
        workspace=workspace,
    )
    donor = donor_export.evidence
    del donor_export
    target = export_revision_evidence(
        checkout,
        target_sha,
        approved_remote_ref,
        workspace=workspace,
    )
    equivalence = _runtime_equivalence_from_revisions(donor, target)
    if not _git_is_ancestor(
        checkout,
        donor_sha,
        target_sha,
        workspace=workspace,
    ) or not _git_is_ancestor(
        checkout,
        OBSERVED_BASELINE_SHA,
        target_sha,
        workspace=workspace,
    ):
        raise ProvenanceError("Git ancestry changed during runtime verification")
    _assert_approved_git_revision(
        checkout,
        target_sha,
        approved_remote_ref,
        temporary_directory=workspace,
    )
    return ReuseGitEvidence(
        donor=donor,
        target=target,
        runtime_equivalence=equivalence,
    )


def _rename_noreplace(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename one relative name without replacing a destination."""

    try:
        _validate_direct_child_name(source_name)
        _validate_direct_child_name(destination_name)
    except ProvenanceError:
        raise _RenameNoreplaceError(errno.EINVAL, "renameat2 name is invalid") from None
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError:
        raise _RenameNoreplaceError(errno.ENOSYS, "renameat2 is unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        source_dir_fd,
        os.fsencode(source_name),
        destination_dir_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise _RenameNoreplaceError(error_number, "renameat2 failed")
        raise OSError(error_number, "renameat2 result is uncertain")


@dataclass(frozen=True, slots=True)
class TreeEntry:
    mode: str
    object_type: str
    object_id: str
    path: str


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    path: str
    mode: str
    data: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _RawTarMember:
    path: str
    type_flag: bytes
    mode: int
    size: int
    pax_path: str | None


@dataclass(frozen=True, slots=True)
class RuntimeInventoryEntry:
    path: str
    mode: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalDigest:
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    components: Mapping[str, CanonicalDigest]
    combined: CanonicalDigest


@dataclass(frozen=True, slots=True)
class RevisionEvidence:
    source_sha: str
    deployment_assets: DeploymentAssets
    runtime_inventory: RuntimeInventory
    dockerfile_mode: str
    dockerfile_blob_sha: str
    dockerfile_sha256: str


@dataclass(frozen=True, slots=True)
class _RevisionExport:
    evidence: RevisionEvidence
    archive: bytes


@dataclass(frozen=True, slots=True)
class ReuseGitEvidence:
    donor: RevisionEvidence
    target: RevisionEvidence
    runtime_equivalence: RuntimeEquivalence


@dataclass(frozen=True, slots=True)
class DeploymentAssets:
    schema: int
    sha256: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SourceBuild:
    schema: int = 1
    kind: str = "source-build"


@dataclass(frozen=True, slots=True)
class DigestPair:
    donor_sha256: str
    target_sha256: str


@dataclass(frozen=True, slots=True)
class DockerfileTransition:
    policy: str
    from_sha256: str
    to_sha256: str


@dataclass(frozen=True, slots=True)
class ReusedFrom:
    source_sha: str
    image_reference: str
    image_id: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeEquivalence:
    schema: int
    policy: str
    donor_copy_inputs_sha256: str
    target_copy_inputs_sha256: str
    components: Mapping[str, DigestPair]
    dockerfile_transition: DockerfileTransition


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeReuse:
    schema: int
    kind: str
    reused_from: ReusedFrom
    runtime_equivalence: RuntimeEquivalence


@dataclass(frozen=True, slots=True)
class LoadedBuildManifest:
    schema: int
    approved_remote_ref: str
    build: SourceBuild | VerifiedRuntimeReuse
    deployment_assets: DeploymentAssets
    docker_daemon_id: str
    image_id: str
    image_reference: str
    source_sha: str
    raw_bytes: bytes
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateSourceBuildProvenance:
    schema: int
    kind: str
    source_manifest_schema: int
    source_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateReuseBuildProvenance:
    schema: int
    kind: str
    source_manifest_sha256: str
    reused_from: ReusedFrom
    runtime_equivalence: RuntimeEquivalence


CandidateBuildProvenance = (
    CandidateSourceBuildProvenance | CandidateReuseBuildProvenance
)


@dataclass(frozen=True, slots=True)
class PublishedManifest:
    source_sha: str
    canonical_bytes: bytes
    safe_read: SafeRead


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    device: int
    inode: int
    uid: int
    mode: int


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    uid: int
    mode: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _OpenedDirectory:
    name: str | None
    path: Path
    descriptor: int
    identity: DirectoryIdentity
    exact_mode: int | None


class RetainedDirectoryRoot(Protocol):
    """Common retained-root interface for trusted relative file reads."""

    path: Path
    effective_uid: int

    @property
    def descriptor(self) -> int: ...

    def revalidate(self) -> None: ...


class CanonicalStateDirectory:
    """Retained, inode-bound canonical deployment-state directory chain."""

    def __init__(
        self,
        *,
        path: Path,
        effective_uid: int,
        components: tuple[_OpenedDirectory, ...],
    ) -> None:
        self.path = path
        self.effective_uid = effective_uid
        self._components = components
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise ProvenanceError("canonical state directory is closed")
        return self._components[-1].descriptor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def revalidate(self) -> None:
        if self._closed:
            raise ProvenanceError("canonical state directory is closed")
        try:
            home = self._components[0]
            if home.path.resolve(strict=True) != home.path:
                raise ProvenanceError("canonical state directory identity changed")
            for index, component in enumerate(self._components):
                if index == 0:
                    listed = os.stat(component.path, follow_symlinks=False)
                else:
                    parent = self._components[index - 1]
                    assert component.name is not None
                    listed = os.stat(
                        component.name,
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                opened = os.fstat(component.descriptor)
                _require_safe_directory(
                    listed,
                    effective_uid=self.effective_uid,
                    exact_mode=component.exact_mode,
                )
                _require_safe_directory(
                    opened,
                    effective_uid=self.effective_uid,
                    exact_mode=component.exact_mode,
                )
                if (
                    _directory_identity(listed) != component.identity
                    or _directory_identity(opened) != component.identity
                ):
                    raise ProvenanceError("canonical state directory identity changed")
        except ProvenanceError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise ProvenanceError(
                "canonical state directory identity changed"
            ) from None

    def fsync(self) -> None:
        self.revalidate()
        try:
            os.fsync(self.descriptor)
        except OSError:
            raise ProvenanceError(
                "canonical state directory could not be synced"
            ) from None
        self.revalidate()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for component in reversed(self._components):
            try:
                os.close(component.descriptor)
            except OSError:
                pass


class CanonicalCheckoutRoot:
    """Retained, inode-bound root of one canonical source checkout."""

    def __init__(
        self,
        *,
        path: Path,
        effective_uid: int,
        descriptor: int,
        identity: DirectoryIdentity,
    ) -> None:
        self.path = path
        self.effective_uid = effective_uid
        self._descriptor = descriptor
        self._identity = identity
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise ProvenanceError("canonical checkout root is closed")
        return self._descriptor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def revalidate(self) -> None:
        if self._closed:
            raise ProvenanceError("canonical checkout root is closed")
        try:
            if self.path.resolve(strict=True) != self.path:
                raise ProvenanceError("canonical checkout root identity changed")
            listed = os.stat(self.path, follow_symlinks=False)
            opened = os.fstat(self._descriptor)
            try:
                _require_safe_directory(
                    listed,
                    effective_uid=self.effective_uid,
                    exact_mode=None,
                )
                _require_safe_directory(
                    opened,
                    effective_uid=self.effective_uid,
                    exact_mode=None,
                )
            except ProvenanceError:
                raise ProvenanceError("canonical checkout root is unsafe") from None
            if (
                _directory_identity(listed) != self._identity
                or _directory_identity(opened) != self._identity
            ):
                raise ProvenanceError("canonical checkout root identity changed")
        except ProvenanceError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise ProvenanceError("canonical checkout root identity changed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._descriptor)
        except OSError:
            pass


def _require_state_root(value: object) -> CanonicalStateDirectory:
    if not isinstance(value, CanonicalStateDirectory):
        raise ProvenanceError("canonical state directory is invalid")
    return value


class SafeRead:
    """Bytes retained from one inode with repeatable pathname validation."""

    def __init__(
        self,
        *,
        data: bytes,
        text: str | None,
        descriptor: int,
        identity: FileIdentity,
        root: RetainedDirectoryRoot,
        parent_components: tuple[_OpenedDirectory, ...],
        name: str,
        expected_mode: int,
        maximum_size: int,
    ) -> None:
        self.data = data
        self.text = text
        self.identity = identity
        self._descriptor = descriptor
        self._root = root
        self._parent_components = parent_components
        self._name = name
        self._expected_mode = expected_mode
        self._maximum_size = maximum_size
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise ProvenanceError("safe file is closed")
        return self._descriptor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _parent_descriptor(self) -> int:
        if self._parent_components:
            return self._parent_components[-1].descriptor
        return self._root.descriptor

    def _revalidate_parent_chain(self) -> int:
        self._root.revalidate()
        parent_descriptor = self._root.descriptor
        for component in self._parent_components:
            assert component.name is not None
            listed_directory = os.stat(
                component.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            opened_directory = os.fstat(component.descriptor)
            _require_safe_directory(
                listed_directory,
                effective_uid=self._root.effective_uid,
                exact_mode=component.exact_mode,
            )
            _require_safe_directory(
                opened_directory,
                effective_uid=self._root.effective_uid,
                exact_mode=component.exact_mode,
            )
            if (
                _directory_identity(listed_directory) != component.identity
                or _directory_identity(opened_directory) != component.identity
            ):
                raise ProvenanceError("safe file directory identity changed")
            parent_descriptor = component.descriptor
        self._root.revalidate()
        return parent_descriptor

    def revalidate(self) -> None:
        if self._closed:
            raise ProvenanceError("safe file is closed")
        try:
            parent_descriptor = self._revalidate_parent_chain()
            listed = os.stat(
                self._name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(self._descriptor)
            _require_safe_file(
                listed,
                effective_uid=self._root.effective_uid,
                expected_mode=self._expected_mode,
                maximum_size=self._maximum_size,
            )
            _require_safe_file(
                opened,
                effective_uid=self._root.effective_uid,
                expected_mode=self._expected_mode,
                maximum_size=self._maximum_size,
            )
            if (
                _file_identity(listed) != self.identity
                or _file_identity(opened) != self.identity
            ):
                raise ProvenanceError("safe file identity changed")
            self._revalidate_parent_chain()
        except ProvenanceError:
            raise
        except OSError:
            raise ProvenanceError("safe file identity changed") from None

    def fsync(self) -> None:
        self.revalidate()
        try:
            os.fsync(self._descriptor)
        except OSError:
            raise ProvenanceError("safe file could not be synced") from None
        self.revalidate()

    def close(self) -> None:
        if self._closed:
            return
        self._close_descriptor_only()
        for component in reversed(self._parent_components):
            try:
                os.close(component.descriptor)
            except OSError:
                pass

    def _close_descriptor_only(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._descriptor)
        except OSError:
            pass


class BuildLock:
    """Retained nonblocking lock for every operation targeting one source SHA."""

    def __init__(
        self,
        *,
        state: CanonicalStateDirectory,
        source_sha: str,
        name: str,
        descriptor: int,
        identity: FileIdentity,
    ) -> None:
        self.state = state
        self.source_sha = source_sha
        self.name = name
        self.identity = identity
        self._descriptor = descriptor
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise ProvenanceError("build lock is closed")
        return self._descriptor

    @classmethod
    def acquire(
        cls,
        state: CanonicalStateDirectory,
        source_sha: str,
    ) -> BuildLock:
        state = _require_state_root(state)
        if (
            not isinstance(source_sha, str)
            or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
        ):
            raise ProvenanceError("build lock source SHA is invalid")
        state.revalidate()
        name = f".build-{source_sha}.lock"
        descriptor = -1
        created = False
        create_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
        )
        existing_flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        listed: os.stat_result | None = None
        try:
            while True:
                try:
                    descriptor = os.open(
                        name,
                        create_flags,
                        0o600,
                        dir_fd=state.descriptor,
                    )
                    created = True
                    break
                except InterruptedError:
                    continue
                except OSError as error:
                    if error.errno != errno.EEXIST:
                        raise
                    break
            if created:
                while True:
                    try:
                        os.fchmod(descriptor, 0o600)
                        break
                    except InterruptedError:
                        continue
                listed = os.stat(
                    name,
                    dir_fd=state.descriptor,
                    follow_symlinks=False,
                )
            else:
                listed = os.stat(
                    name,
                    dir_fd=state.descriptor,
                    follow_symlinks=False,
                )
                while True:
                    try:
                        descriptor = os.open(
                            name,
                            existing_flags,
                            dir_fd=state.descriptor,
                        )
                        break
                    except InterruptedError:
                        continue
            opened = os.fstat(descriptor)
            _require_safe_file(
                listed,
                effective_uid=state.effective_uid,
                expected_mode=0o600,
                maximum_size=0,
            )
            _require_safe_file(
                opened,
                effective_uid=state.effective_uid,
                expected_mode=0o600,
                maximum_size=0,
            )
            identity = _file_identity(opened)
            if _file_identity(listed) != identity:
                raise ProvenanceError("build lock identity changed")
            while True:
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except InterruptedError:
                    continue
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN}:
                        raise ProvenanceError(
                            "target operation already in progress"
                        ) from None
                    raise
            result = cls(
                state=state,
                source_sha=source_sha,
                name=name,
                descriptor=descriptor,
                identity=identity,
            )
            descriptor = -1
            try:
                result.revalidate()
            except BaseException:
                result.close()
                raise
            return result
        except ProvenanceError:
            raise
        except OSError:
            raise ProvenanceError("build lock is unsafe") from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: object,
        _value: object,
        _traceback: object,
    ) -> None:
        if exception_type is None:
            try:
                self.revalidate()
            finally:
                self.close()
            return
        try:
            self.revalidate()
        except ProvenanceError:
            self.close()
            return
        self.close()

    def revalidate(self) -> None:
        if self._closed:
            raise ProvenanceError("build lock is closed")
        try:
            self.state.revalidate()
            listed = os.stat(
                self.name,
                dir_fd=self.state.descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(self._descriptor)
            _require_safe_file(
                listed,
                effective_uid=self.state.effective_uid,
                expected_mode=0o600,
                maximum_size=0,
            )
            _require_safe_file(
                opened,
                effective_uid=self.state.effective_uid,
                expected_mode=0o600,
                maximum_size=0,
            )
            if (
                _file_identity(listed) != self.identity
                or _file_identity(opened) != self.identity
            ):
                raise ProvenanceError("build lock identity changed")
            self.state.revalidate()
        except ProvenanceError:
            raise
        except OSError:
            raise ProvenanceError("build lock identity changed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._descriptor)
        except OSError:
            pass


def _directory_identity(metadata: os.stat_result) -> DirectoryIdentity:
    return DirectoryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
        link_count=metadata.st_nlink,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _same_file_across_rename(before: FileIdentity, after: FileIdentity) -> bool:
    return (
        before.device == after.device
        and before.inode == after.inode
        and before.uid == after.uid
        and before.mode == after.mode
        and before.link_count == after.link_count
        and before.size == after.size
        and before.mtime_ns == after.mtime_ns
    )


def _rename_failure_is_definitely_uncommitted(
    state: CanonicalStateDirectory,
    source_name: str,
    destination_name: str,
    descriptor: int,
    staged_identity: FileIdentity,
) -> bool:
    """Prove an EEXIST result left the staged inode at its source name."""

    try:
        state.revalidate()
        listed_source = os.stat(
            source_name,
            dir_fd=state.descriptor,
            follow_symlinks=False,
        )
        opened_source = os.fstat(descriptor)
        listed_destination = os.stat(
            destination_name,
            dir_fd=state.descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(listed_source) != staged_identity
            or _file_identity(opened_source) != staged_identity
            or (listed_destination.st_dev, listed_destination.st_ino)
            == (staged_identity.device, staged_identity.inode)
        ):
            return False
        state.revalidate()
        return True
    except (OSError, ProvenanceError):
        return False


def _require_safe_directory(
    metadata: os.stat_result,
    *,
    effective_uid: int,
    exact_mode: int | None,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != effective_uid
        or mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise ProvenanceError("canonical state directory is unsafe")


def _require_safe_file(
    metadata: os.stat_result,
    *,
    effective_uid: int,
    expected_mode: int,
    maximum_size: int,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != effective_uid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_nlink != 1
        or metadata.st_size < 0
        or metadata.st_size > maximum_size
    ):
        raise ProvenanceError("safe file metadata is invalid")


def _validate_direct_child_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or os.path.basename(name) != name
        or "\x00" in name
    ):
        raise ProvenanceError("safe file name is invalid")


def _read_exact_file(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        try:
            chunk = os.read(descriptor, remaining)
        except InterruptedError:
            continue
        if not chunk:
            raise ProvenanceError("safe file changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    while True:
        try:
            extra = os.read(descriptor, 1)
            break
        except InterruptedError:
            continue
    if extra:
        raise ProvenanceError("safe file changed while reading")
    return b"".join(chunks)


def _safe_read_child(
    parent: RetainedDirectoryRoot,
    parent_components: tuple[_OpenedDirectory, ...],
    name: str,
    *,
    expected_mode: int,
    maximum_size: int,
    require_utf8: bool = True,
) -> SafeRead:
    _validate_direct_child_name(name)
    if (
        type(expected_mode) is not int
        or not 0 <= expected_mode <= 0o777
        or type(maximum_size) is not int
        or maximum_size < 0
        or type(require_utf8) is not bool
    ):
        raise ProvenanceError("safe file read policy is invalid")
    parent.revalidate()
    parent_descriptor = (
        parent_components[-1].descriptor if parent_components else parent.descriptor
    )
    descriptor = -1
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        listed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        _require_safe_file(
            listed,
            effective_uid=parent.effective_uid,
            expected_mode=expected_mode,
            maximum_size=maximum_size,
        )
        _require_safe_file(
            opened,
            effective_uid=parent.effective_uid,
            expected_mode=expected_mode,
            maximum_size=maximum_size,
        )
        identity = _file_identity(opened)
        if _file_identity(listed) != identity:
            raise ProvenanceError("safe file identity changed before reading")
        data = _read_exact_file(descriptor, opened.st_size)
        text: str | None = None
        if require_utf8:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ProvenanceError("safe file is not valid UTF-8") from None
        result = SafeRead(
            data=data,
            text=text,
            descriptor=descriptor,
            identity=identity,
            root=parent,
            parent_components=parent_components,
            name=name,
            expected_mode=expected_mode,
            maximum_size=maximum_size,
        )
        descriptor = -1
        try:
            result.revalidate()
        except BaseException:
            result._close_descriptor_only()
            raise
        return result
    except ProvenanceError:
        raise
    except OSError:
        raise ProvenanceError("safe file could not be read") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_directory_component(
    *,
    parent_descriptor: int,
    name: str,
    path: Path,
    effective_uid: int,
    exact_mode: int | None,
) -> _OpenedDirectory:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        listed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError:
        raise ProvenanceError("canonical state directory is unavailable") from None
    try:
        opened = os.fstat(descriptor)
        _require_safe_directory(
            listed,
            effective_uid=effective_uid,
            exact_mode=exact_mode,
        )
        _require_safe_directory(
            opened,
            effective_uid=effective_uid,
            exact_mode=exact_mode,
        )
        identity = _directory_identity(opened)
        if _directory_identity(listed) != identity:
            raise ProvenanceError("canonical state directory identity changed")
        return _OpenedDirectory(
            name=name,
            path=path,
            descriptor=descriptor,
            identity=identity,
            exact_mode=exact_mode,
        )
    except BaseException as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if isinstance(error, ProvenanceError):
            raise
        if isinstance(error, OSError):
            raise ProvenanceError("canonical state directory is unavailable") from None
        raise


def open_canonical_state_directory() -> CanonicalStateDirectory:
    """Open the sole deployment state directory derived from the passwd home."""

    effective_uid = os.geteuid()
    try:
        raw_home = pwd.getpwuid(effective_uid).pw_dir
    except (KeyError, OSError):
        raise ProvenanceError("canonical state directory is unavailable") from None

    if not isinstance(raw_home, str) or not raw_home:
        raise ProvenanceError("canonical state directory is unsafe")
    home_path = Path(raw_home)
    if not home_path.is_absolute():
        raise ProvenanceError("canonical state directory is unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    components: list[_OpenedDirectory] = []
    try:
        if home_path.resolve(strict=True) != home_path:
            raise ProvenanceError("canonical state directory is unsafe")
        listed_home = os.stat(home_path, follow_symlinks=False)
        home_descriptor = os.open(home_path, flags)
        try:
            opened_home = os.fstat(home_descriptor)
            _require_safe_directory(
                listed_home,
                effective_uid=effective_uid,
                exact_mode=None,
            )
            _require_safe_directory(
                opened_home,
                effective_uid=effective_uid,
                exact_mode=None,
            )
            home_identity = _directory_identity(opened_home)
            if _directory_identity(listed_home) != home_identity:
                raise ProvenanceError("canonical state directory identity changed")
            components.append(
                _OpenedDirectory(
                    name=None,
                    path=home_path,
                    descriptor=home_descriptor,
                    identity=home_identity,
                    exact_mode=None,
                )
            )
        except BaseException:
            os.close(home_descriptor)
            raise

        current_path = home_path
        for name, exact_mode in (
            ("apps", None),
            ("commerce-ops-desk", None),
            ("deploy-state", 0o700),
        ):
            current_path = current_path / name
            components.append(
                _open_directory_component(
                    parent_descriptor=components[-1].descriptor,
                    name=name,
                    path=current_path,
                    effective_uid=effective_uid,
                    exact_mode=exact_mode,
                )
            )
        state = CanonicalStateDirectory(
            path=current_path,
            effective_uid=effective_uid,
            components=tuple(components),
        )
        state.revalidate()
        return state
    except ProvenanceError:
        for component in reversed(components):
            try:
                os.close(component.descriptor)
            except OSError:
                pass
        raise
    except (OSError, RuntimeError, ValueError):
        for component in reversed(components):
            try:
                os.close(component.descriptor)
            except OSError:
                pass
        raise ProvenanceError("canonical state directory is unavailable") from None


def open_canonical_checkout_root(repository: Path) -> CanonicalCheckoutRoot:
    """Open one absolute, canonical, current-user-owned checkout root."""

    if not isinstance(repository, Path) or not repository.is_absolute():
        raise ProvenanceError("canonical checkout root is invalid")
    effective_uid = os.geteuid()
    descriptor = -1
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        if repository.resolve(strict=True) != repository:
            raise ProvenanceError("canonical checkout root is invalid")
        listed = os.stat(repository, follow_symlinks=False)
        descriptor = os.open(repository, flags)
        opened = os.fstat(descriptor)
        try:
            _require_safe_directory(
                listed,
                effective_uid=effective_uid,
                exact_mode=None,
            )
            _require_safe_directory(
                opened,
                effective_uid=effective_uid,
                exact_mode=None,
            )
        except ProvenanceError:
            raise ProvenanceError("canonical checkout root is unsafe") from None
        identity = _directory_identity(opened)
        if _directory_identity(listed) != identity:
            raise ProvenanceError("canonical checkout root identity changed")
        result = CanonicalCheckoutRoot(
            path=repository,
            effective_uid=effective_uid,
            descriptor=descriptor,
            identity=identity,
        )
        descriptor = -1
        try:
            result.revalidate()
        except BaseException:
            result.close()
            raise
        return result
    except ProvenanceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProvenanceError("canonical checkout root is unavailable") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def safe_read_direct_child(
    parent: RetainedDirectoryRoot,
    name: str,
    *,
    expected_mode: int,
    maximum_size: int,
    require_utf8: bool = True,
) -> SafeRead:
    """Safely read one direct child while retaining its exact descriptor."""

    return _safe_read_child(
        parent,
        (),
        name,
        expected_mode=expected_mode,
        maximum_size=maximum_size,
        require_utf8=require_utf8,
    )


def safe_read_nested_child(
    root: RetainedDirectoryRoot,
    relative_path: str,
    *,
    expected_mode: int,
    maximum_size: int,
    require_utf8: bool = True,
) -> SafeRead:
    """Safely open every component of one canonical relative file path."""

    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path.startswith("/")
        or "\x00" in relative_path
    ):
        raise ProvenanceError("safe file path is invalid")
    parts = relative_path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ProvenanceError("safe file path is invalid")
    root.revalidate()
    opened: list[_OpenedDirectory] = []
    parent_descriptor = root.descriptor
    current_path = root.path
    try:
        for part in parts[:-1]:
            current_path = current_path / part
            try:
                component = _open_directory_component(
                    parent_descriptor=parent_descriptor,
                    name=part,
                    path=current_path,
                    effective_uid=root.effective_uid,
                    exact_mode=None,
                )
            except ProvenanceError:
                raise ProvenanceError("safe file directory is unsafe") from None
            opened.append(component)
            parent_descriptor = component.descriptor
        result = _safe_read_child(
            root,
            tuple(opened),
            parts[-1],
            expected_mode=expected_mode,
            maximum_size=maximum_size,
            require_utf8=require_utf8,
        )
        opened = []
        return result
    finally:
        for component in reversed(opened):
            try:
                os.close(component.descriptor)
            except OSError:
                pass


def safe_read_deployment_asset(
    root: CanonicalCheckoutRoot,
    relative_path: str,
) -> SafeRead:
    """Read one approved deployment asset under fixed public-file policy."""

    if not isinstance(root, CanonicalCheckoutRoot):
        raise ProvenanceError("deployment asset checkout root is invalid")
    if (
        not isinstance(relative_path, str)
        or relative_path not in DEPLOYMENT_ASSET_PATHS
    ):
        raise ProvenanceError("deployment asset path is invalid")
    return safe_read_nested_child(
        root,
        relative_path,
        expected_mode=0o644,
        maximum_size=1024 * 1024,
        require_utf8=True,
    )


def canonical_manifest_path(
    state: CanonicalStateDirectory,
    source_sha: str,
) -> Path:
    state = _require_state_root(state)
    state.revalidate()
    if (
        not isinstance(source_sha, str)
        or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
    ):
        raise ProvenanceError("build manifest path is invalid")
    return state.path / f"build-{source_sha}.json"


def require_canonical_manifest_path(
    state: CanonicalStateDirectory,
    supplied_path: Path,
    source_sha: str,
) -> str:
    state = _require_state_root(state)
    expected = canonical_manifest_path(state, source_sha)
    if not supplied_path.is_absolute() or supplied_path != expected:
        raise ProvenanceError("build manifest path is invalid")
    state.revalidate()
    return expected.name


def source_sha_from_manifest_path(
    state: CanonicalStateDirectory,
    supplied_path: Path,
) -> str:
    state = _require_state_root(state)
    state.revalidate()
    if not supplied_path.is_absolute() or supplied_path.parent != state.path:
        raise ProvenanceError("build manifest path is invalid")
    match = MANIFEST_NAME_PATTERN.fullmatch(supplied_path.name)
    if match is None:
        raise ProvenanceError("build manifest path is invalid")
    state.revalidate()
    return match.group(1)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError("JSON data is invalid")
        result[key] = value
    return result


def _reject_nonstandard_number(_value: str) -> object:
    raise ProvenanceError("JSON data is invalid")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ProvenanceError("JSON data is invalid")
    return parsed


def strict_json_loads(raw: bytes) -> object:
    """Decode one UTF-8 JSON value while rejecting ambiguous extensions."""

    if type(raw) is not bytes:
        raise ProvenanceError("JSON data is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvenanceError("JSON data is invalid") from None
    if text.startswith("\ufeff"):
        raise ProvenanceError("JSON data is invalid")
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonstandard_number,
            parse_float=_parse_finite_float,
        )
    except ProvenanceError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise ProvenanceError("JSON data is invalid") from None


def canonical_manifest_bytes(payload: object) -> bytes:
    """Serialize a schema-3 manifest in its one accepted byte encoding."""

    try:
        serialized = json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise ProvenanceError("manifest data is not serializable") from None
    return f"{serialized}\n".encode()


def canonical_json_bytes(payload: object) -> bytes:
    """Serialize provenance data as compact canonical JSON without a newline."""

    try:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise ProvenanceError("provenance data is not serializable") from None
    return serialized.encode("utf-8")


def length_framed_sha256(domain: bytes, payload: bytes) -> str:
    """Hash one unambiguous domain-separated byte payload."""

    if type(domain) is not bytes or type(payload) is not bytes:
        raise ProvenanceError("digest input is invalid")
    try:
        length = len(payload).to_bytes(8, byteorder="big", signed=False)
    except OverflowError:
        raise ProvenanceError("digest input is invalid") from None
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(length)
    digest.update(payload)
    return digest.hexdigest()


def _parse_deployment_assets(value: object) -> DeploymentAssets:
    if not isinstance(value, dict) or set(value) != DEPLOYMENT_ASSET_FIELDS:
        raise ProvenanceError("build manifest schema is invalid")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise ProvenanceError("build manifest schema is invalid")
    digests = value.get("sha256")
    if not isinstance(digests, dict) or set(digests) != DEPLOYMENT_ASSET_PATHS:
        raise ProvenanceError("build manifest schema is invalid")
    if any(
        not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
        for digest in digests.values()
    ):
        raise ProvenanceError("build manifest schema is invalid")
    return DeploymentAssets(
        schema=cast(int, value["schema"]),
        sha256=MappingProxyType(cast(dict[str, str], dict(digests))),
    )


def _valid_remote_ref(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("refs/remotes/"):
        return False
    suffix = value.removeprefix("refs/remotes/")
    components = suffix.split("/")
    return not (
        not suffix
        or len(components) < 2
        or any(
            not component
            or component.startswith(".")
            or component.endswith((".", ".lock"))
            for component in components
        )
        or ".." in suffix
        or "@{" in suffix
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or any(character in "~^:?*[\\" for character in value)
    )


def _validate_manifest_identity(payload: Mapping[str, object]) -> None:
    source_sha = payload.get("source_sha")
    image_reference = payload.get("image_reference")
    image_id = payload.get("image_id")
    daemon_id = payload.get("docker_daemon_id")
    if (
        not isinstance(source_sha, str)
        or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
    ):
        raise ProvenanceError("build manifest identity is invalid")
    if image_reference != f"commerce-ops-desk:{source_sha}":
        raise ProvenanceError("build manifest identity is invalid")
    if not isinstance(image_id, str) or IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise ProvenanceError("build manifest identity is invalid")
    if not _valid_remote_ref(payload.get("approved_remote_ref")):
        raise ProvenanceError("build manifest identity is invalid")
    if (
        not isinstance(daemon_id, str)
        or not 1 <= len(daemon_id) <= 256
        or any(ord(character) < 33 or ord(character) > 126 for character in daemon_id)
    ):
        raise ProvenanceError("build manifest identity is invalid")


def _parse_reuse_build(
    value: Mapping[str, object],
    *,
    target_source_sha: str | None,
) -> VerifiedRuntimeReuse:
    try:
        reused_value = value["reused_from"]
        equivalence_value = value["runtime_equivalence"]
        if set(value) != REUSE_BUILD_FIELDS:
            raise ProvenanceError("build manifest schema is invalid")
        if (
            type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("kind") != "verified-runtime-reuse"
        ):
            raise ProvenanceError("build manifest schema is invalid")
        if (
            not isinstance(reused_value, dict)
            or set(reused_value) != REUSED_FROM_FIELDS
            or not isinstance(equivalence_value, dict)
            or set(equivalence_value) != RUNTIME_EQUIVALENCE_FIELDS
        ):
            raise ProvenanceError("build manifest schema is invalid")
        reused = cast(dict[str, object], reused_value)
        equivalence = cast(dict[str, object], equivalence_value)
        donor_source_sha = reused.get("source_sha")
        donor_image_reference = reused.get("image_reference")
        donor_image_id = reused.get("image_id")
        donor_manifest_sha256 = reused.get("manifest_sha256")
        if (
            not isinstance(donor_source_sha, str)
            or SOURCE_SHA_PATTERN.fullmatch(donor_source_sha) is None
            or (target_source_sha is not None and donor_source_sha == target_source_sha)
            or donor_image_reference != f"commerce-ops-desk:{donor_source_sha}"
            or not isinstance(donor_image_id, str)
            or IMAGE_ID_PATTERN.fullmatch(donor_image_id) is None
            or not isinstance(donor_manifest_sha256, str)
            or SHA256_PATTERN.fullmatch(donor_manifest_sha256) is None
        ):
            raise ProvenanceError("build manifest identity is invalid")
        if type(equivalence.get("schema")) is not int or equivalence["schema"] != 1:
            raise ProvenanceError("build manifest schema is invalid")
        raw_components_value = equivalence["components"]
        transition_value = equivalence["dockerfile_transition"]
        if (
            not isinstance(raw_components_value, dict)
            or set(raw_components_value) != RUNTIME_COMPONENTS
            or not isinstance(transition_value, dict)
            or set(transition_value) != DOCKERFILE_TRANSITION_FIELDS
        ):
            raise ProvenanceError("build manifest schema is invalid")
        raw_components = cast(dict[str, object], raw_components_value)
        transition = cast(dict[str, object], transition_value)
        if any(
            not isinstance(pair, dict) or set(pair) != DIGEST_PAIR_FIELDS
            for pair in raw_components.values()
        ):
            raise ProvenanceError("build manifest schema is invalid")
        donor_copy_digest = equivalence.get("donor_copy_inputs_sha256")
        target_copy_digest = equivalence.get("target_copy_inputs_sha256")
        if (
            equivalence.get("policy") != RUNTIME_EQUIVALENCE_POLICY
            or not isinstance(donor_copy_digest, str)
            or SHA256_PATTERN.fullmatch(donor_copy_digest) is None
            or target_copy_digest != donor_copy_digest
        ):
            raise ProvenanceError("build manifest runtime evidence is invalid")
        for pair_value in raw_components.values():
            pair = cast(dict[str, object], pair_value)
            donor_digest = pair.get("donor_sha256")
            target_digest = pair.get("target_sha256")
            if (
                not isinstance(donor_digest, str)
                or SHA256_PATTERN.fullmatch(donor_digest) is None
                or target_digest != donor_digest
            ):
                raise ProvenanceError("build manifest runtime evidence is invalid")
        if (
            transition.get("policy") != DOCKERFILE_TRANSITION_POLICY
            or transition.get("from_sha256") != DONOR_DOCKERFILE_SHA256
            or transition.get("to_sha256") != TARGET_DOCKERFILE_SHA256
        ):
            raise ProvenanceError("build manifest runtime evidence is invalid")
        components = {
            name: DigestPair(
                donor_sha256=cast(dict[str, str], pair)["donor_sha256"],
                target_sha256=cast(dict[str, str], pair)["target_sha256"],
            )
            for name, pair in raw_components.items()
        }
        return VerifiedRuntimeReuse(
            schema=value["schema"],
            kind=cast(str, value["kind"]),
            reused_from=ReusedFrom(
                source_sha=cast(str, reused["source_sha"]),
                image_reference=cast(str, reused["image_reference"]),
                image_id=cast(str, reused["image_id"]),
                manifest_sha256=cast(str, reused["manifest_sha256"]),
            ),
            runtime_equivalence=RuntimeEquivalence(
                schema=equivalence["schema"],
                policy=cast(str, equivalence["policy"]),
                donor_copy_inputs_sha256=cast(
                    str, equivalence["donor_copy_inputs_sha256"]
                ),
                target_copy_inputs_sha256=cast(
                    str, equivalence["target_copy_inputs_sha256"]
                ),
                components=MappingProxyType(components),
                dockerfile_transition=DockerfileTransition(
                    policy=cast(str, transition["policy"]),
                    from_sha256=cast(str, transition["from_sha256"]),
                    to_sha256=cast(str, transition["to_sha256"]),
                ),
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise ProvenanceError("build manifest schema is invalid") from None


def parse_build_manifest_bytes(raw: bytes) -> LoadedBuildManifest:
    """Parse a build manifest into an immutable provenance value."""

    decoded = strict_json_loads(raw)
    if not isinstance(decoded, dict):
        raise ProvenanceError("build manifest schema is invalid")
    payload = cast(dict[str, object], decoded)
    schema = payload.get("schema")
    if type(schema) is not int or schema not in (2, 3):
        raise ProvenanceError("build manifest schema is invalid")
    expected_fields = LEGACY_MANIFEST_FIELDS if schema == 2 else SCHEMA3_MANIFEST_FIELDS
    if set(payload) != expected_fields:
        raise ProvenanceError("build manifest schema is invalid")
    build_identity: SourceBuild | VerifiedRuntimeReuse = SourceBuild()
    if schema == 3:
        build = payload["build"]
        if not isinstance(build, dict):
            raise ProvenanceError("build manifest schema is invalid")
        if build.get("kind") == "source-build":
            if (
                set(build) != SOURCE_BUILD_FIELDS
                or type(build.get("schema")) is not int
                or build["schema"] != 1
            ):
                raise ProvenanceError("build manifest schema is invalid")
        elif build.get("kind") == "verified-runtime-reuse":
            build_identity = _parse_reuse_build(
                build,
                target_source_sha=cast(str, payload["source_sha"]),
            )
        else:
            raise ProvenanceError("build manifest schema is invalid")
    _validate_manifest_identity(payload)
    if (
        isinstance(build_identity, VerifiedRuntimeReuse)
        and build_identity.reused_from.image_id == payload["image_id"]
    ):
        raise ProvenanceError("build manifest identity is invalid")
    try:
        deployment_assets = _parse_deployment_assets(payload["deployment_assets"])
    except (KeyError, TypeError, ValueError):
        raise ProvenanceError("build manifest schema is invalid") from None
    if schema == 3 and canonical_manifest_bytes(payload) != raw:
        raise ProvenanceError("schema-3 build manifest is not canonical")
    try:
        return LoadedBuildManifest(
            schema=schema,
            approved_remote_ref=cast(str, payload["approved_remote_ref"]),
            build=build_identity,
            deployment_assets=deployment_assets,
            docker_daemon_id=cast(str, payload["docker_daemon_id"]),
            image_id=cast(str, payload["image_id"]),
            image_reference=cast(str, payload["image_reference"]),
            source_sha=cast(str, payload["source_sha"]),
            raw_bytes=raw,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (KeyError, TypeError, ValueError):
        raise ProvenanceError("build manifest schema is invalid") from None


def _reused_from_payload(value: ReusedFrom) -> dict[str, object]:
    if not isinstance(value, ReusedFrom):
        raise ProvenanceError("candidate build provenance is invalid")
    return {
        "source_sha": value.source_sha,
        "image_reference": value.image_reference,
        "image_id": value.image_id,
        "manifest_sha256": value.manifest_sha256,
    }


def _snapshot_mapping(value: object, *, error: str) -> dict[object, object]:
    if not isinstance(value, Mapping):
        raise ProvenanceError(error)
    try:
        return dict(value.items())
    except Exception:  # noqa: BLE001 - custom Mapping methods may raise anything.
        raise ProvenanceError(error) from None


def _runtime_equivalence_payload(value: RuntimeEquivalence) -> dict[str, object]:
    if not isinstance(value, RuntimeEquivalence):
        raise ProvenanceError("candidate build provenance is invalid")
    components_value = _snapshot_mapping(
        value.components,
        error="candidate build provenance is invalid",
    )
    if set(components_value) != RUNTIME_COMPONENTS or not isinstance(
        value.dockerfile_transition, DockerfileTransition
    ):
        raise ProvenanceError("candidate build provenance is invalid")
    components: dict[str, object] = {}
    for name in sorted(RUNTIME_COMPONENTS):
        pair = components_value[name]
        if not isinstance(pair, DigestPair):
            raise ProvenanceError("candidate build provenance is invalid")
        components[name] = {
            "donor_sha256": pair.donor_sha256,
            "target_sha256": pair.target_sha256,
        }
    return {
        "schema": value.schema,
        "policy": value.policy,
        "donor_copy_inputs_sha256": value.donor_copy_inputs_sha256,
        "target_copy_inputs_sha256": value.target_copy_inputs_sha256,
        "components": components,
        "dockerfile_transition": {
            "policy": value.dockerfile_transition.policy,
            "from_sha256": value.dockerfile_transition.from_sha256,
            "to_sha256": value.dockerfile_transition.to_sha256,
        },
    }


def _candidate_build_provenance_payload_unchecked(
    value: CandidateBuildProvenance,
) -> dict[str, object]:
    if isinstance(value, CandidateSourceBuildProvenance):
        return {
            "schema": value.schema,
            "kind": value.kind,
            "source_manifest_schema": value.source_manifest_schema,
            "source_manifest_sha256": value.source_manifest_sha256,
        }
    if isinstance(value, CandidateReuseBuildProvenance):
        return {
            "schema": value.schema,
            "kind": value.kind,
            "source_manifest_sha256": value.source_manifest_sha256,
            "reused_from": _reused_from_payload(value.reused_from),
            "runtime_equivalence": _runtime_equivalence_payload(
                value.runtime_equivalence
            ),
        }
    raise ProvenanceError("candidate build provenance is invalid")


def parse_candidate_build_provenance(value: object) -> CandidateBuildProvenance:
    """Parse one exact candidate-state provenance arm into an immutable value."""

    if not isinstance(value, dict):
        raise ProvenanceError("candidate build provenance is invalid")
    try:
        kind = value.get("kind")
        if kind == "source-build":
            if set(value) != CANDIDATE_SOURCE_PROVENANCE_FIELDS:
                raise ProvenanceError("candidate build provenance is invalid")
            schema = value.get("schema")
            manifest_schema = value.get("source_manifest_schema")
            manifest_sha256 = value.get("source_manifest_sha256")
            if (
                type(schema) is not int
                or schema != 1
                or type(manifest_schema) is not int
                or manifest_schema not in (2, 3)
                or not isinstance(manifest_sha256, str)
                or SHA256_PATTERN.fullmatch(manifest_sha256) is None
            ):
                raise ProvenanceError("candidate build provenance is invalid")
            return CandidateSourceBuildProvenance(
                schema=schema,
                kind="source-build",
                source_manifest_schema=manifest_schema,
                source_manifest_sha256=manifest_sha256,
            )
        if kind == "verified-runtime-reuse":
            if set(value) != CANDIDATE_REUSE_PROVENANCE_FIELDS:
                raise ProvenanceError("candidate build provenance is invalid")
            manifest_sha256 = value.get("source_manifest_sha256")
            if (
                not isinstance(manifest_sha256, str)
                or SHA256_PATTERN.fullmatch(manifest_sha256) is None
            ):
                raise ProvenanceError("candidate build provenance is invalid")
            build_payload = dict(value)
            del build_payload["source_manifest_sha256"]
            try:
                build = _parse_reuse_build(
                    build_payload,
                    target_source_sha=None,
                )
            except ProvenanceError:
                raise ProvenanceError("candidate build provenance is invalid") from None
            return CandidateReuseBuildProvenance(
                schema=build.schema,
                kind=build.kind,
                source_manifest_sha256=manifest_sha256,
                reused_from=build.reused_from,
                runtime_equivalence=build.runtime_equivalence,
            )
    except ProvenanceError:
        raise
    except (KeyError, TypeError, ValueError):
        pass
    raise ProvenanceError("candidate build provenance is invalid")


def _require_loaded_build_manifest(value: object) -> LoadedBuildManifest:
    """Rebind a typed manifest value to its exact validated raw bytes."""

    if not isinstance(value, LoadedBuildManifest):
        raise ProvenanceError("loaded build manifest is invalid")
    try:
        reparsed = parse_build_manifest_bytes(value.raw_bytes)
    except ProvenanceError:
        raise ProvenanceError("loaded build manifest is invalid") from None
    if reparsed != value:
        raise ProvenanceError("loaded build manifest is invalid")
    return reparsed


def candidate_build_provenance(
    manifest: LoadedBuildManifest,
) -> CandidateBuildProvenance:
    """Derive candidate provenance only from one strictly loaded manifest."""

    try:
        manifest = _require_loaded_build_manifest(manifest)
    except ProvenanceError:
        raise ProvenanceError(
            "candidate build provenance requires a loaded manifest"
        ) from None
    if isinstance(manifest.build, SourceBuild):
        value: CandidateBuildProvenance = CandidateSourceBuildProvenance(
            schema=1,
            kind="source-build",
            source_manifest_schema=manifest.schema,
            source_manifest_sha256=manifest.raw_sha256,
        )
    elif isinstance(manifest.build, VerifiedRuntimeReuse):
        value = CandidateReuseBuildProvenance(
            schema=manifest.build.schema,
            kind=manifest.build.kind,
            source_manifest_sha256=manifest.raw_sha256,
            reused_from=manifest.build.reused_from,
            runtime_equivalence=manifest.build.runtime_equivalence,
        )
    else:  # pragma: no cover - LoadedBuildManifest constructor invariant.
        raise ProvenanceError("candidate build provenance is invalid")
    return parse_candidate_build_provenance(
        _candidate_build_provenance_payload_unchecked(value)
    )


def candidate_build_provenance_payload(
    value: CandidateBuildProvenance,
) -> dict[str, object]:
    """Return the exact JSON-native representation of typed provenance."""

    payload = _candidate_build_provenance_payload_unchecked(value)
    if parse_candidate_build_provenance(payload) != value:
        raise ProvenanceError("candidate build provenance is invalid")
    return payload


def candidate_build_provenance_digest(
    value: CandidateBuildProvenance,
) -> CanonicalDigest:
    """Canonicalize and hash candidate provenance with its fixed domain."""

    canonical = canonical_json_bytes(candidate_build_provenance_payload(value))
    return CanonicalDigest(
        canonical_bytes=canonical,
        sha256=length_framed_sha256(
            CANDIDATE_BUILD_PROVENANCE_DOMAIN,
            canonical,
        ),
    )


def expected_reserved_provenance_labels(
    manifest: LoadedBuildManifest,
) -> Mapping[str, str]:
    """Return the complete reserved label set for one manifest arm."""

    try:
        manifest = _require_loaded_build_manifest(manifest)
    except ProvenanceError:
        raise ProvenanceError("provenance label manifest is invalid")
    if isinstance(manifest.build, SourceBuild):
        expected = {} if manifest.schema == 2 else {BUILD_KIND_LABEL: "source-build"}
    elif isinstance(manifest.build, VerifiedRuntimeReuse):
        expected = {
            BUILD_KIND_LABEL: "verified-runtime-reuse",
            REUSED_FROM_SOURCE_LABEL: manifest.build.reused_from.source_sha,
            REUSED_FROM_IMAGE_ID_LABEL: manifest.build.reused_from.image_id,
            RUNTIME_INPUTS_SHA256_LABEL: (
                manifest.build.runtime_equivalence.target_copy_inputs_sha256
            ),
        }
    else:  # pragma: no cover - LoadedBuildManifest constructor invariant.
        raise ProvenanceError("provenance label manifest is invalid")
    return MappingProxyType(expected)


def validate_reserved_provenance_labels(
    manifest: LoadedBuildManifest,
    labels: object,
) -> None:
    """Require the exact schema-specific subset of reserved image labels."""

    label_values = _snapshot_mapping(labels, error="provenance labels are invalid")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in label_values.items()
    ):
        raise ProvenanceError("provenance labels are invalid") from None
    validated_labels = cast(dict[str, str], label_values)
    reserved = {
        key: value
        for key, value in validated_labels.items()
        if key.startswith(RESERVED_PROVENANCE_LABEL_PREFIX)
    }
    if reserved != dict(expected_reserved_provenance_labels(manifest)):
        raise ProvenanceError("provenance labels do not match the build manifest")


def validate_candidate_manifest_chain(
    target: LoadedBuildManifest,
    donor: LoadedBuildManifest | None,
) -> None:
    """Bind a target arm to no donor or to its exact source-build donor."""

    try:
        target = _require_loaded_build_manifest(target)
    except ProvenanceError:
        raise ProvenanceError("candidate manifest chain is invalid")
    if isinstance(target.build, SourceBuild):
        if donor is not None:
            raise ProvenanceError("source build must not have a donor manifest")
        return
    if not isinstance(target.build, VerifiedRuntimeReuse):
        raise ProvenanceError("candidate manifest chain is invalid")
    try:
        donor = _require_loaded_build_manifest(donor)
    except ProvenanceError:
        raise ProvenanceError(
            "reuse requires one source-build donor manifest"
        ) from None
    if not isinstance(donor.build, SourceBuild):
        raise ProvenanceError("reuse requires one source-build donor manifest")
    reused = target.build.reused_from
    if (
        donor.source_sha != reused.source_sha
        or donor.image_reference != reused.image_reference
        or donor.image_id != reused.image_id
        or donor.raw_sha256 != reused.manifest_sha256
        or donor.docker_daemon_id != target.docker_daemon_id
    ):
        raise ProvenanceError("donor manifest does not match reuse provenance")


def _fsync_retry(descriptor: int) -> None:
    while True:
        try:
            os.fsync(descriptor)
            return
        except InterruptedError:
            continue


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "manifest write made no progress")
        offset += written


def _validated_publishable_manifest(
    manifest: LoadedBuildManifest,
) -> LoadedBuildManifest:
    if not isinstance(manifest, LoadedBuildManifest):
        raise ProvenanceError("publishable build manifest is invalid")
    parsed = parse_build_manifest_bytes(manifest.raw_bytes)
    if parsed.schema != 3 or parsed != manifest:
        raise ProvenanceError("publishable build manifest is invalid")
    if len(parsed.raw_bytes) > 256 * 1024:
        raise ProvenanceError("publishable build manifest is invalid")
    return parsed


def _verify_temporary_manifest(
    state: CanonicalStateDirectory,
    name: str,
    descriptor: int,
    expected_size: int,
) -> FileIdentity:
    listed = os.stat(
        name,
        dir_fd=state.descriptor,
        follow_symlinks=False,
    )
    opened = os.fstat(descriptor)
    _require_safe_file(
        listed,
        effective_uid=state.effective_uid,
        expected_mode=0o600,
        maximum_size=expected_size,
    )
    _require_safe_file(
        opened,
        effective_uid=state.effective_uid,
        expected_mode=0o600,
        maximum_size=expected_size,
    )
    identity = _file_identity(opened)
    if identity.size != expected_size or _file_identity(listed) != identity:
        raise ProvenanceError("temporary build manifest identity changed")
    return identity


def _cleanup_temporary_manifest(
    state: CanonicalStateDirectory,
    name: str | None,
    descriptor: int,
) -> bool:
    if name is None or descriptor < 0:
        return True
    try:
        listed = os.stat(
            name,
            dir_fd=state.descriptor,
            follow_symlinks=False,
        )
        opened = os.fstat(descriptor)
        if (
            stat.S_ISREG(listed.st_mode)
            and stat.S_ISREG(opened.st_mode)
            and listed.st_uid == state.effective_uid
            and opened.st_uid == state.effective_uid
            and listed.st_nlink == 1
            and opened.st_nlink == 1
            and (listed.st_dev, listed.st_ino) == (opened.st_dev, opened.st_ino)
        ):
            while True:
                try:
                    os.unlink(name, dir_fd=state.descriptor)
                    break
                except InterruptedError:
                    continue
            _fsync_retry(state.descriptor)
            return True
        return False
    except (OSError, ProvenanceError):
        return False


def require_manifest_publication_support(
    state: CanonicalStateDirectory,
) -> None:
    """Prove RENAME_NOREPLACE behavior on the retained state filesystem."""

    state = _require_state_root(state)
    token = secrets.token_hex(16)
    source_name = f".rename-noreplace-probe-{token}-source.tmp"
    destination_name = f".rename-noreplace-probe-{token}-destination.tmp"
    source_descriptor = -1
    destination_descriptor = -1
    source_cleanup_required = False
    destination_cleanup_required = False
    probe_succeeded = False
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        state.revalidate()
        for name, slot in ((source_name, "source"), (destination_name, "destination")):
            descriptor = -1
            while True:
                try:
                    descriptor = os.open(
                        name,
                        flags,
                        0o600,
                        dir_fd=state.descriptor,
                    )
                    break
                except InterruptedError:
                    continue
            if slot == "source":
                source_descriptor = descriptor
                source_cleanup_required = True
            else:
                destination_descriptor = descriptor
                destination_cleanup_required = True
            while True:
                try:
                    os.fchmod(descriptor, 0o600)
                    break
                except InterruptedError:
                    continue
        source_identity = _verify_temporary_manifest(
            state,
            source_name,
            source_descriptor,
            0,
        )
        destination_identity = _verify_temporary_manifest(
            state,
            destination_name,
            destination_descriptor,
            0,
        )
        _fsync_retry(source_descriptor)
        try:
            _rename_noreplace(
                state.descriptor,
                source_name,
                state.descriptor,
                destination_name,
            )
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
        else:
            raise OSError(errno.EIO, "renameat2 replaced an existing destination")
        if (
            _file_identity(
                os.stat(
                    source_name,
                    dir_fd=state.descriptor,
                    follow_symlinks=False,
                )
            )
            != source_identity
            or _file_identity(
                os.stat(
                    destination_name,
                    dir_fd=state.descriptor,
                    follow_symlinks=False,
                )
            )
            != destination_identity
        ):
            raise OSError(errno.EIO, "renameat2 no-clobber behavior is invalid")
        if not _cleanup_temporary_manifest(
            state,
            destination_name,
            destination_descriptor,
        ):
            raise OSError(errno.EIO, "renameat2 probe cleanup is incomplete")
        destination_cleanup_required = False
        _rename_noreplace(
            state.descriptor,
            source_name,
            state.descriptor,
            destination_name,
        )
        moved = os.stat(
            destination_name,
            dir_fd=state.descriptor,
            follow_symlinks=False,
        )
        if not _same_file_across_rename(source_identity, _file_identity(moved)):
            raise OSError(errno.EIO, "renameat2 move behavior is invalid")
        try:
            os.stat(
                source_name,
                dir_fd=state.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise OSError(errno.EIO, "renameat2 retained the source name")
        _fsync_retry(source_descriptor)
        _fsync_retry(state.descriptor)
        state.revalidate()
        probe_succeeded = True
    except ProvenanceError:
        raise ProvenanceError(
            "no-clobber manifest publication is not supported"
        ) from None
    except OSError:
        raise ProvenanceError(
            "no-clobber manifest publication is not supported"
        ) from None
    finally:
        cleanup_complete = True
        for required, descriptor in (
            (source_cleanup_required, source_descriptor),
            (destination_cleanup_required, destination_descriptor),
        ):
            if not required:
                continue
            descriptor_cleaned = False
            for name in (source_name, destination_name):
                if _cleanup_temporary_manifest(state, name, descriptor):
                    descriptor_cleaned = True
                    break
            cleanup_complete = cleanup_complete and descriptor_cleaned
        for descriptor in (destination_descriptor, source_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if probe_succeeded and not cleanup_complete:
            raise ProvenanceError(
                "no-clobber manifest publication is not supported"
            ) from None


def publish_manifest_no_clobber(
    state: CanonicalStateDirectory,
    manifest: LoadedBuildManifest,
) -> PublishedManifest:
    """Durably publish one canonical schema-3 manifest without replacement."""

    state = _require_state_root(state)
    parsed = _validated_publishable_manifest(manifest)
    canonical_bytes = parsed.raw_bytes
    destination_name = f"build-{parsed.source_sha}.json"
    temporary_name: str | None = None
    descriptor = -1
    safe_read: SafeRead | None = None
    stage = "staged"
    try:
        state.revalidate()
        temporary_name = f".{destination_name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        while True:
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=state.descriptor,
                )
                break
            except InterruptedError:
                continue
        while True:
            try:
                os.fchmod(descriptor, 0o600)
                break
            except InterruptedError:
                continue
        _write_all(descriptor, canonical_bytes)
        staged_identity = _verify_temporary_manifest(
            state,
            temporary_name,
            descriptor,
            len(canonical_bytes),
        )
        _fsync_retry(descriptor)
        state.revalidate()
        stage = "rename-in-flight"
        try:
            _rename_noreplace(
                state.descriptor,
                temporary_name,
                state.descriptor,
                destination_name,
            )
        except _RenameNoreplaceError:
            stage = "staged"
            raise
        except OSError as error:
            if (
                error.errno == errno.EEXIST
                and _rename_failure_is_definitely_uncommitted(
                    state,
                    temporary_name,
                    destination_name,
                    descriptor,
                    staged_identity,
                )
            ):
                stage = "staged"
            raise
        stage = "committed"
        post_rename = os.fstat(descriptor)
        _require_safe_file(
            post_rename,
            effective_uid=state.effective_uid,
            expected_mode=0o600,
            maximum_size=len(canonical_bytes),
        )
        if post_rename.st_size != len(canonical_bytes):
            raise ProvenanceError("published build manifest identity changed")
        post_rename_identity = _file_identity(post_rename)
        if not _same_file_across_rename(staged_identity, post_rename_identity):
            raise ProvenanceError("published build manifest identity changed")
        listed_destination = os.stat(
            destination_name,
            dir_fd=state.descriptor,
            follow_symlinks=False,
        )
        if _file_identity(listed_destination) != post_rename_identity:
            raise ProvenanceError("published build manifest identity changed")
        _fsync_retry(descriptor)
        state.fsync()
        safe_read = safe_read_direct_child(
            state,
            destination_name,
            expected_mode=0o600,
            maximum_size=256 * 1024,
        )
        if (
            safe_read.data != canonical_bytes
            or safe_read.identity != post_rename_identity
        ):
            raise ProvenanceError("published build manifest bytes changed")
        safe_read.revalidate()
        result = PublishedManifest(
            source_sha=parsed.source_sha,
            canonical_bytes=canonical_bytes,
            safe_read=safe_read,
        )
        safe_read = None
        return result
    except BaseException as error:
        if safe_read is not None:
            safe_read.close()
        if stage != "staged":
            raise ManifestPublicationIndeterminate(
                "build manifest publication is indeterminate"
            ) from None
        cleanup_complete = _cleanup_temporary_manifest(
            state,
            temporary_name,
            descriptor,
        )
        if not isinstance(error, Exception):
            raise
        message = "build manifest could not be published"
        if not cleanup_complete:
            message += "; temporary cleanup is incomplete"
        raise ManifestPublishError(
            message,
            cleanup_complete=cleanup_complete,
        ) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def confirm_existing_manifest_durable(
    state: CanonicalStateDirectory,
    manifest: LoadedBuildManifest,
) -> PublishedManifest:
    """Confirm exact pre-existing manifest bytes and repair their durability."""

    state = _require_state_root(state)
    parsed = _validated_publishable_manifest(manifest)
    name = f"build-{parsed.source_sha}.json"
    opened = safe_read_direct_child(
        state,
        name,
        expected_mode=0o600,
        maximum_size=256 * 1024,
    )
    safe_read: SafeRead | None = opened
    try:
        if opened.data != parsed.raw_bytes:
            raise ProvenanceError("existing build manifest bytes do not match")
        opened.fsync()
        state.fsync()
        opened.revalidate()
        result = PublishedManifest(
            source_sha=parsed.source_sha,
            canonical_bytes=parsed.raw_bytes,
            safe_read=opened,
        )
        safe_read = None
        return result
    finally:
        if safe_read is not None:
            safe_read.close()
