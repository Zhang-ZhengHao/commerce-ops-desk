"""Security contracts for the workstation Caddy transaction lock."""

from __future__ import annotations

import builtins
import errno
import fcntl as real_fcntl
import importlib.util
import os as real_os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_caddy_lock", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class FileEntry:
    mode: int
    uid: int
    gid: int
    nlink: int = 1
    device: int = 41
    inode: int = 101

    def metadata(self) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=self.mode,
            st_uid=self.uid,
            st_gid=self.gid,
            st_nlink=self.nlink,
            st_dev=self.device,
            st_ino=self.inode,
        )


def regular_entry(
    *,
    uid: int = 0,
    gid: int = 0,
    mode: int = 0o644,
    nlink: int = 1,
    inode: int = 101,
) -> FileEntry:
    return FileEntry(
        mode=stat.S_IFREG | mode,
        uid=uid,
        gid=gid,
        nlink=nlink,
        inode=inode,
    )


class FakeOS:
    """Small descriptor-aware filesystem used by both bootstrap and helper."""

    O_RDONLY = real_os.O_RDONLY
    O_WRONLY = real_os.O_WRONLY
    O_RDWR = real_os.O_RDWR
    O_CREAT = real_os.O_CREAT
    O_EXCL = real_os.O_EXCL
    O_CLOEXEC = getattr(real_os, "O_CLOEXEC", 0)
    O_NOFOLLOW = getattr(real_os, "O_NOFOLLOW", 0)

    def __init__(self, entry: FileEntry | None, *, effective_gid: int) -> None:
        self.path_entry = entry
        self.effective_gid = effective_gid
        self.descriptors: dict[int, FileEntry] = {}
        self.closed_descriptors: list[int] = []
        self._next_descriptor = 20

    def getegid(self) -> int:
        return self.effective_gid

    def geteuid(self) -> int:
        return 0

    def open(self, _path: object, flags: int, mode: int = 0o777) -> int:
        if flags & self.O_CREAT and flags & self.O_EXCL:
            if self.path_entry is not None:
                raise FileExistsError(errno.EEXIST, "already exists")
            self.path_entry = regular_entry(uid=0, gid=0, mode=mode)
        elif self.path_entry is None:
            raise FileNotFoundError(errno.ENOENT, "missing")

        assert self.path_entry is not None
        if stat.S_ISLNK(self.path_entry.mode) and flags & self.O_NOFOLLOW:
            raise OSError(errno.ELOOP, "symbolic link refused")

        descriptor = self._next_descriptor
        self._next_descriptor += 1
        self.descriptors[descriptor] = self.path_entry
        return descriptor

    def close(self, descriptor: int) -> None:
        if descriptor not in self.descriptors:
            raise OSError(errno.EBADF, "bad descriptor")
        del self.descriptors[descriptor]
        self.closed_descriptors.append(descriptor)

    def lstat(self, _path: object) -> SimpleNamespace:
        if self.path_entry is None:
            raise FileNotFoundError(errno.ENOENT, "missing")
        return self.path_entry.metadata()

    def stat(self, _path: object, *, follow_symlinks: bool = True) -> SimpleNamespace:
        if self.path_entry is None:
            raise FileNotFoundError(errno.ENOENT, "missing")
        if stat.S_ISLNK(self.path_entry.mode) and not follow_symlinks:
            return self.path_entry.metadata()
        return self.path_entry.metadata()

    def fstat(self, descriptor: int) -> SimpleNamespace:
        return self.descriptors[descriptor].metadata()

    def fchown(self, descriptor: int, uid: int, gid: int) -> None:
        entry = self.descriptors[descriptor]
        if uid != -1:
            entry.uid = uid
        if gid != -1:
            entry.gid = gid

    def fchmod(self, descriptor: int, mode: int) -> None:
        entry = self.descriptors[descriptor]
        entry.mode = stat.S_IFMT(entry.mode) | mode

    def chown(
        self,
        _path: object,
        uid: int,
        gid: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del follow_symlinks
        assert self.path_entry is not None
        if uid != -1:
            self.path_entry.uid = uid
        if gid != -1:
            self.path_entry.gid = gid

    def chmod(self, _path: object, mode: int, *, follow_symlinks: bool = True) -> None:
        del follow_symlinks
        assert self.path_entry is not None
        self.path_entry.mode = stat.S_IFMT(self.path_entry.mode) | mode

    def fsync(self, _descriptor: int) -> None:
        return None


class FakeLockPath:
    def __init__(self, fake_os: FakeOS) -> None:
        self.fake_os = fake_os

    def __str__(self) -> str:
        return "/run/lock/commerce-ops-desk-caddy.lock"

    def __fspath__(self) -> str:
        return str(self)

    def lstat(self) -> SimpleNamespace:
        return self.fake_os.lstat(self)


class BootstrapRunner:
    """Execute the embedded root bootstrap against FakeOS, never sudo."""

    def __init__(self, module: ModuleType, fake_os: FakeOS) -> None:
        self.module = module
        self.fake_os = fake_os
        self.commands: list[list[str]] = []

    def run(
        self, arguments: Any, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.commands.append(command)
        assert command[:4] == [
            self.module.SUDO_BINARY,
            self.module.PYTHON_BINARY,
            "-I",
            "-c",
        ]
        bootstrap = command[4]
        fake_sys = SimpleNamespace(argv=["-c", *command[5:]])
        original_import = builtins.__import__

        def controlled_import(
            name: str,
            globals_: Mapping[str, object] | None = None,
            locals_: Mapping[str, object] | None = None,
            fromlist: Sequence[str] | None = (),
            level: int = 0,
        ) -> Any:
            if name == "os":
                return self.fake_os
            if name == "stat":
                return stat
            if name == "sys":
                return fake_sys
            return original_import(name, globals_, locals_, fromlist, level)

        controlled_builtins = dict(vars(builtins))
        controlled_builtins["__import__"] = controlled_import
        try:
            exec(  # noqa: S102 - intentionally exercise the captured fixed script
                compile(bootstrap, "<caddy-lock-bootstrap>", "exec"),
                {"__builtins__": controlled_builtins},
                {},
            )
        except SystemExit as error:
            raise self.module.DeploymentError(
                f"Caddy transaction lock bootstrap rejected metadata: {error}"
            ) from error
        return subprocess.CompletedProcess(command, 0, "", "")


class FakeFcntl:
    LOCK_EX = real_fcntl.LOCK_EX
    LOCK_NB = real_fcntl.LOCK_NB
    LOCK_UN = real_fcntl.LOCK_UN

    def __init__(
        self,
        *,
        busy_attempts: int = 0,
        always_busy: bool = False,
        require_nonblocking: bool = False,
        on_acquire: Any | None = None,
    ) -> None:
        self.busy_attempts = busy_attempts
        self.always_busy = always_busy
        self.require_nonblocking = require_nonblocking
        self.on_acquire = on_acquire
        self.acquire_calls: list[int] = []
        self.unlock_calls: list[int] = []
        self.acquired = False

    def flock(self, descriptor: int, operation: int) -> None:
        if operation & self.LOCK_UN:
            self.unlock_calls.append(descriptor)
            self.acquired = False
            return

        self.acquire_calls.append(operation)
        if self.require_nonblocking and not operation & self.LOCK_NB:
            raise AssertionError("Caddy lock acquisition must use LOCK_NB")
        if self.always_busy or len(self.acquire_calls) <= self.busy_attempts:
            raise BlockingIOError(errno.EWOULDBLOCK, "lock is busy")
        self.acquired = True
        if self.on_acquire is not None:
            self.on_acquire(descriptor)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        assert duration > 0
        self.sleeps.append(duration)
        self.now += duration


def install_lock_fakes(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry: FileEntry | None,
    effective_gid: int,
    fake_fcntl: FakeFcntl | None = None,
    fake_time: FakeTime | None = None,
) -> tuple[FakeOS, BootstrapRunner, FakeFcntl, FakeTime]:
    fake_os = FakeOS(entry, effective_gid=effective_gid)
    runner = BootstrapRunner(module, fake_os)
    selected_fcntl = fake_fcntl or FakeFcntl()
    selected_time = fake_time or FakeTime()
    monkeypatch.setattr(module, "os", fake_os)
    monkeypatch.setattr(module, "fcntl", selected_fcntl)
    monkeypatch.setattr(module, "time", selected_time)
    monkeypatch.setattr(module, "CADDY_TRANSACTION_LOCK", FakeLockPath(fake_os))
    monkeypatch.setattr(
        module,
        "_activate_caddy_mutation_fence",
        lambda _runner, _token: None,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_deactivate_caddy_mutation_fence",
        lambda _runner, _token: None,
        raising=False,
    )
    return fake_os, runner, selected_fcntl, selected_time


def assert_hardened_metadata(entry: FileEntry | None, *, gid: int) -> None:
    assert entry is not None
    assert stat.S_ISREG(entry.mode)
    assert entry.uid == 0
    assert entry.gid == gid
    assert stat.S_IMODE(entry.mode) == 0o640
    assert entry.nlink == 1


def test_lock_bootstrap_creates_root_deploy_group_0640_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    deploy_gid = 2_345
    fake_os, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=None,
        effective_gid=deploy_gid,
    )

    with module._caddy_transaction_lock(runner):
        assert_hardened_metadata(fake_os.path_entry, gid=deploy_gid)

    assert runner.commands[0][-2:] == [
        str(module.CADDY_TRANSACTION_LOCK),
        str(deploy_gid),
    ]
    assert fake_os.descriptors == {}


def test_lock_bootstrap_migrates_only_exact_legacy_root_root_0644_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    deploy_gid = 2_345
    fake_os, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=regular_entry(uid=0, gid=0, mode=0o644),
        effective_gid=deploy_gid,
    )

    with module._caddy_transaction_lock(runner):
        assert_hardened_metadata(fake_os.path_entry, gid=deploy_gid)

    assert runner.commands[0][-1] == str(deploy_gid)
    assert fake_os.descriptors == {}


def test_lock_bootstrap_accepts_existing_root_deploy_group_0640_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    deploy_gid = 2_345
    fake_os, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=regular_entry(uid=0, gid=deploy_gid, mode=0o640),
        effective_gid=deploy_gid,
    )

    with module._caddy_transaction_lock(runner):
        assert_hardened_metadata(fake_os.path_entry, gid=deploy_gid)

    assert fake_os.descriptors == {}


def test_transaction_lock_fences_root_mutators_for_the_entire_critical_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fake_os, runner, fake_fcntl, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=regular_entry(uid=0, gid=2_345, mode=0o640),
        effective_gid=2_345,
    )
    events: list[str] = []

    def activate(received_runner: object, token: str) -> None:
        assert received_runner is runner
        assert fake_fcntl.acquired is True
        assert len(token) == 64 and set(token) <= set("0123456789abcdef")
        events.append(f"activate:{token}")

    def deactivate(received_runner: object, token: str) -> None:
        assert received_runner is runner
        assert fake_fcntl.acquired is True
        assert runner._caddy_mutation_fence_token == token
        events.append(f"deactivate:{token}")

    monkeypatch.setattr(module, "_activate_caddy_mutation_fence", activate)
    monkeypatch.setattr(module, "_deactivate_caddy_mutation_fence", deactivate)

    with module._caddy_transaction_lock(runner):
        token = runner._caddy_mutation_fence_token
        events.append(f"body:{token}")

    assert events == [
        f"activate:{token}",
        f"body:{token}",
        f"deactivate:{token}",
    ]
    assert not hasattr(runner, "_caddy_mutation_fence_token")
    assert fake_os.descriptors == {}


def test_root_mutation_fence_uses_a_blocking_isolated_root_barrier() -> None:
    module = load_deploy_tool()

    class CaptureRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], object]] = []

        def run(
            self,
            arguments: Sequence[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.calls.append((command, kwargs.get("timeout")))
            return subprocess.CompletedProcess(command, 0, "", "")

    runner = CaptureRunner()
    token = "a" * 64

    module._activate_caddy_mutation_fence(runner, token)
    module._deactivate_caddy_mutation_fence(runner, token)

    assert len(runner.calls) == 2
    for (command, timeout), action in zip(
        runner.calls,
        ("activate", "deactivate"),
        strict=True,
    ):
        assert command[:4] == [
            module.SUDO_BINARY,
            module.PYTHON_BINARY,
            "-I",
            "-c",
        ]
        helper = command[4]
        compile(helper, f"<mutation-fence-{action}>", "exec")
        assert "fcntl.flock" in helper
        assert "LOCK_EX" in helper
        assert "O_NOFOLLOW" in helper
        assert "os.fsync" in helper
        assert command[-4:] == [
            action,
            token,
            str(module.CADDY_MUTATION_LOCK),
            str(module.CADDY_MUTATION_FENCE),
        ]
        assert timeout is None


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(regular_entry(uid=1_000, gid=2_345, mode=0o640), id="wrong-uid"),
        pytest.param(regular_entry(uid=0, gid=2_346, mode=0o640), id="wrong-gid"),
        pytest.param(regular_entry(uid=0, gid=2_345, mode=0o660), id="wrong-mode"),
        pytest.param(
            regular_entry(uid=0, gid=2_345, mode=0o640, nlink=2),
            id="multiple-links",
        ),
        pytest.param(
            FileEntry(
                mode=stat.S_IFLNK | 0o777,
                uid=0,
                gid=2_345,
                nlink=1,
            ),
            id="symbolic-link",
        ),
    ],
)
def test_lock_bootstrap_rejects_unsafe_existing_metadata(
    monkeypatch: pytest.MonkeyPatch,
    entry: FileEntry,
) -> None:
    module = load_deploy_tool()
    _, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=replace(entry),
        effective_gid=2_345,
    )

    with (
        pytest.raises(module.DeploymentError, match="rejected|unsafe"),
        module._caddy_transaction_lock(runner),
    ):
        pytest.fail("unsafe lock metadata reached the transaction body")


def test_lock_contention_times_out_after_bounded_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fake_fcntl = FakeFcntl(always_busy=True, require_nonblocking=True)
    fake_time = FakeTime()
    fake_os, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=None,
        effective_gid=0,
        fake_fcntl=fake_fcntl,
        fake_time=fake_time,
    )

    with (
        pytest.raises(module.DeploymentError, match="timed out|timeout"),
        module._caddy_transaction_lock(runner),
    ):
        pytest.fail("a permanently busy lock reached the transaction body")

    assert fake_fcntl.acquire_calls
    assert all(call & fake_fcntl.LOCK_NB for call in fake_fcntl.acquire_calls)
    assert 9.9 <= fake_time.now <= 10.001
    assert sum(fake_time.sleeps) <= 10.001
    assert fake_fcntl.unlock_calls == []
    assert fake_os.descriptors == {}


def test_lock_contention_succeeds_when_owner_releases_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fake_fcntl = FakeFcntl(busy_attempts=2, require_nonblocking=True)
    fake_time = FakeTime()
    fake_os, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=None,
        effective_gid=0,
        fake_fcntl=fake_fcntl,
        fake_time=fake_time,
    )

    entered = False
    with module._caddy_transaction_lock(runner):
        entered = True
        assert fake_fcntl.acquired

    assert entered
    assert len(fake_fcntl.acquire_calls) == 3
    assert all(call & fake_fcntl.LOCK_NB for call in fake_fcntl.acquire_calls)
    assert 0 < fake_time.now < 10
    assert len(fake_fcntl.unlock_calls) == 1
    assert fake_os.descriptors == {}


def test_lock_rejects_path_inode_swap_after_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    fake_os: FakeOS

    def replace_lock_path(_descriptor: int) -> None:
        assert fake_os.path_entry is not None
        fake_os.path_entry = replace(fake_os.path_entry, inode=9_999)

    fake_fcntl = FakeFcntl(
        require_nonblocking=True,
        on_acquire=replace_lock_path,
    )
    fake_os, runner, _, _ = install_lock_fakes(
        module,
        monkeypatch,
        entry=None,
        effective_gid=0,
        fake_fcntl=fake_fcntl,
    )

    with (
        pytest.raises(module.DeploymentError, match="changed|inode|unsafe"),
        module._caddy_transaction_lock(runner),
    ):
        pytest.fail("swapped lock path reached the transaction body")

    assert len(fake_fcntl.unlock_calls) == 1
    assert fake_os.descriptors == {}
