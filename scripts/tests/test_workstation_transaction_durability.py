"""Durability contracts for immutable Caddy transaction publication."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_transaction_durability", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def test_transaction_directory_creation_fsyncs_every_parent_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory = (
        tmp_path / "var" / "lib" / "commerce-ops-desk" / "caddy-transactions"
    )
    monkeypatch.setattr(
        module,
        "CADDY_TRANSACTION_DIRECTORY",
        transaction_directory,
    )
    monkeypatch.setattr(
        module,
        "_assert_root_owned_transaction_path",
        lambda *_args, **_kwargs: None,
    )
    runner = RecordingRunner()

    module._ensure_caddy_transaction_directory(runner)

    helper_calls = [
        command
        for command in runner.calls
        if module.PYTHON_BINARY in command and "-c" in command
    ]
    assert len(helper_calls) == 1
    helper_call = helper_calls[0]
    python_index = helper_call.index(module.PYTHON_BINARY)
    script_index = helper_call.index("-c", python_index + 1)
    assert "-I" in helper_call[python_index + 1 : script_index]
    helper = helper_call[script_index + 1]
    compile(helper, "<transaction-directory-durability-helper>", "exec")
    assert helper.count("os.fsync") >= 3
    assert "os.lstat" in helper
    assert "os.fstat" in helper
    assert "O_NOFOLLOW" in helper
    assert helper_call[-3:] == [
        str(transaction_directory.parents[1]),
        str(transaction_directory.parent),
        str(transaction_directory),
    ]
    install_index = next(
        index
        for index, command in enumerate(runner.calls)
        if Path(command[1]).name == "install" and "-d" in command
    )
    assert install_index < runner.calls.index(helper_call)


def test_immutable_publication_fsyncs_before_and_after_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    transaction_directory = tmp_path / "root" / "transactions"
    source = tmp_path / "source.json"
    target = transaction_directory / (
        "commerce-ops-desk.20261009T120000Z-0123456789abcdef0123456789abcdef.json"
    )
    source.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "CADDY_TRANSACTION_DIRECTORY",
        transaction_directory,
    )
    runner = RecordingRunner()

    module._install_root_owned_immutable_file(runner, source, target)

    helper_calls = [
        command
        for command in runner.calls
        if module.PYTHON_BINARY in command and "-c" in command
    ]
    assert len(helper_calls) == 2
    publication_helper, cleanup_helper = helper_calls

    for label, helper_call in (
        ("publication", publication_helper),
        ("cleanup", cleanup_helper),
    ):
        python_index = helper_call.index(module.PYTHON_BINARY)
        script_index = helper_call.index("-c", python_index + 1)
        assert "-I" in helper_call[python_index + 1 : script_index]
        helper = helper_call[script_index + 1]
        compile(helper, f"<immutable-transaction-{label}-helper>", "exec")
        assert "os.fsync" in helper
        assert "os.fstat" in helper
        assert str(target) in helper_call
        assert str(transaction_directory) in helper_call

    publication_script = publication_helper[publication_helper.index("-c") + 1]
    cleanup_script = cleanup_helper[cleanup_helper.index("-c") + 1]
    assert publication_script.count("os.fsync") >= 2
    assert "st_nlink != 2" in publication_script
    assert "st_nlink != 1" in cleanup_script

    link_index = next(
        index
        for index, command in enumerate(runner.calls)
        if Path(command[1]).name == "ln"
    )
    publication_helper_index = runner.calls.index(publication_helper)
    cleanup_index = next(
        index
        for index, command in enumerate(runner.calls)
        if Path(command[1]).name == "rm"
    )
    cleanup_helper_index = runner.calls.index(cleanup_helper)
    assert link_index < publication_helper_index < cleanup_index < cleanup_helper_index
