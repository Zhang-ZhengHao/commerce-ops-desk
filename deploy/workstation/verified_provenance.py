"""Shared provenance primitives for verified builds and deployments."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
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
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_NAME_PATTERN = re.compile(r"build-([0-9a-f]{40})\.json\Z")
RUNTIME_EQUIVALENCE_POLICY = "runtime-copy-inputs-v1+pip-timeout-300-v1"
DOCKERFILE_TRANSITION_POLICY = "pip-timeout-300-v1"
DONOR_DOCKERFILE_SHA256 = (
    "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a"
)
TARGET_DOCKERFILE_SHA256 = (
    "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
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
    target_source_sha: str,
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
            or donor_source_sha == target_source_sha
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
