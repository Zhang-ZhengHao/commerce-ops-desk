"""Trust-boundary contracts for privileged workstation deployment commands."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
RUNBOOK = PRODUCT_ROOT / "deploy" / "workstation" / "RUNBOOK.md"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_root_trust_boundary", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class InvocationCaptured(RuntimeError):
    """Stop a privileged helper before it can touch the host filesystem."""


class CapturingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, arguments: Sequence[str], **_kwargs: Any) -> None:
        self.commands.append(list(arguments))
        raise InvocationCaptured


def test_caddy_lock_runs_embedded_root_python_in_isolated_mode() -> None:
    module = load_deploy_tool()
    runner = CapturingRunner()

    with pytest.raises(InvocationCaptured), module._caddy_transaction_lock(runner):
        pytest.fail("the privileged bootstrap unexpectedly yielded")

    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert command[0] == module.SUDO_BINARY
    python_index = command.index(module.PYTHON_BINARY)
    script_index = command.index("-c", python_index + 1)
    assert "-I" in command[python_index + 1 : script_index]


def test_command_runner_gives_every_sudo_process_a_fixed_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    observed_environments: list[object] = []

    def capture_run(
        arguments: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed_environments.append(kwargs.get("env"))
        return subprocess.CompletedProcess(list(arguments), 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", capture_run)
    monkeypatch.setenv("PYTHONPATH", "/tmp/attacker-controlled-python")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/attacker-controlled-library.so")

    runner = module.CommandRunner()
    runner.run([module.SUDO_BINARY, "true"])
    runner.run(
        [module.SUDO_BINARY, "true"],
        environment={
            "PATH": "/tmp/attacker-controlled-bin",
            "LC_ALL": "en_US.UTF-8",
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": "/tmp/attacker-controlled-python",
            "DEPLOY_SECRET": "must-not-cross-the-root-boundary",
        },
    )

    fixed_environment: Mapping[str, str] = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
    }
    assert observed_environments == [fixed_environment, fixed_environment]


def test_command_runner_resolves_only_allowlisted_absolute_sudo_subcommands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    observed_commands: list[list[str]] = []

    def capture_run(
        arguments: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed_commands.append(list(arguments))
        return subprocess.CompletedProcess(list(arguments), 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", capture_run)
    runner = module.CommandRunner()

    runner.run([module.SUDO_BINARY, "test", "-f", "/trusted/path"])
    runner.run([module.SUDO_BINARY, "cat", "--", "/trusted/path"])

    assert observed_commands == [
        [module.SUDO_BINARY, "/usr/bin/test", "-f", "/trusted/path"],
        [module.SUDO_BINARY, "/usr/bin/cat", "--", "/trusted/path"],
    ]
    with pytest.raises(module.DeploymentError, match="sudo|privileged|allowlist"):
        runner.run([module.SUDO_BINARY, "attacker-controlled-command"])


def test_runbook_describes_the_deployment_operator_as_part_of_the_tcb() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert re.search(
        r"deployment operator.{0,120}(?:trusted computing base|\bTCB\b)",
        runbook,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_runbook_does_not_overstate_the_root_boundary() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8").lower()

    assert re.search(r"fixed\s+root helper", runbook) is None
    assert re.search(r"protect against\s+the deployment user", runbook) is None
