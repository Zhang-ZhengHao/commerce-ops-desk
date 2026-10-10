"""Static contracts for the versioned workstation deployment assets."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PRODUCT_ROOT / "deploy" / "workstation" / "compose.yaml"
CADDY_TEMPLATE = (
    PRODUCT_ROOT / "deploy" / "workstation" / "commerce-ops-desk.Caddyfile.template"
)
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
RUNBOOK = PRODUCT_ROOT / "deploy" / "workstation" / "RUNBOOK.md"
WORKFLOW = PRODUCT_ROOT / ".github" / "workflows" / "verify.yml"
MAKEFILE = PRODUCT_ROOT / "Makefile"
DESIGN_SUMMARY = PRODUCT_ROOT / "docs" / "design-summary.md"
HARDENING_DESIGN = (
    PRODUCT_ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-10-09-workstation-deployment-hardening-design.md"
)
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
DOCKER_DAEMON_ID = "local-daemon-id"
CADDY_LINUX_AMD64_IMAGE = (
    "caddy:2.6.2-alpine@"
    "sha256:7992b931b7da3cf0840dd69ea74b2c67d423faf03408da8abdc31b7590a239a7"
)
DEPLOYMENT_ASSET_PATHS = (
    "deploy/workstation/compose.yaml",
    "deploy/workstation/commerce-ops-desk.Caddyfile.template",
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template",
)


def create_candidate_prepare_lock(app_root: Path) -> Path:
    lock_path = app_root / "deploy-state" / f"candidate-{VALID_SHA[:12]}.prepare.lock"
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o600)
    return lock_path


class InlineSudoPythonRunner:
    """Execute the exact isolated sudo-Python helper as the test user."""

    def __init__(
        self,
        module: ModuleType,
        *,
        before_execute: Callable[[list[str]], None] | None = None,
        rewrite_stdout: Callable[[str], str] | None = None,
    ) -> None:
        self.module = module
        self.before_execute = before_execute
        self.rewrite_stdout = rewrite_stdout
        self.calls: list[list[str]] = []
        self.results: list[subprocess.CompletedProcess[str]] = []

    def run(
        self,
        arguments: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append(command)
        assert command[:5] == [
            self.module.SUDO_BINARY,
            self.module.PYTHON_BINARY,
            "-I",
            "-c",
            self.module.CANDIDATE_DATA_QUARANTINE_HELPER,
        ]
        if self.before_execute is not None:
            self.before_execute(command)
        result = subprocess.run(
            [sys.executable, *command[2:]],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.results.append(result)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise self.module.DeploymentError(
                f"command failed ({command[0]}, exit {result.returncode}){suffix}"
            )
        if self.rewrite_stdout is None:
            return result
        return subprocess.CompletedProcess(
            command,
            result.returncode,
            self.rewrite_stdout(result.stdout),
            result.stderr,
        )


def quarantine_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
    )


def prepare_candidate_quarantine_inputs(
    module: ModuleType,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_state: bool = False,
) -> tuple[Path, Path, Path, Path]:
    app_root = root / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    create_candidate_prepare_lock(app_root)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)
    retained = data_path / ".retained"
    retained.write_text("keep\n", encoding="utf-8")
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    if include_state:
        state_path.write_text('{"keep": true}\n', encoding="utf-8")
        state_path.chmod(0o600)
    return app_root, state_directory, data_path, state_path


@pytest.fixture
def rename_noreplace_path() -> Iterator[Path]:
    """Use local tmpfs because the shared NFS rejects RENAME_NOREPLACE."""

    root = Path(tempfile.mkdtemp(prefix="commerce-ops-quarantine-", dir="/dev/shm"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def deployment_asset_payload(root: Path = PRODUCT_ROOT) -> dict[str, object]:
    return {
        "schema": 1,
        "sha256": {
            relative_path: hashlib.sha256(
                (root / relative_path).read_bytes()
            ).hexdigest()
            for relative_path in DEPLOYMENT_ASSET_PATHS
        },
    }


def write_deployment_assets(root: Path) -> dict[str, str]:
    contents = {
        DEPLOYMENT_ASSET_PATHS[0]: "services:\n  commerce-ops-desk:\n    image: demo\n",
        DEPLOYMENT_ASSET_PATHS[1]: (
            "http://commerce-ops-desk.srrsh.aig.rest {\n"
            "\treverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n"
            "}\n"
        ),
        DEPLOYMENT_ASSET_PATHS[2]: (
            "http://commerce-ops-desk.srrsh.aig.rest {\n"
            "\treverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n"
            "}\n"
        ),
    }
    for relative_path, content in contents.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return contents


def candidate_state_payload(
    module: ModuleType,
    *,
    product_root: Path,
    temporary_root: Path,
) -> dict[str, object]:
    identity = module.candidate_identity(VALID_SHA)
    return {
        "schema": 2,
        "source_sha": VALID_SHA,
        "project_name": identity.project_name,
        "container_name": identity.container_name,
        "container_id": "1" * 64,
        "image": identity.image,
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "candidate_port": 18088,
        "candidate_data_dir": str(temporary_root / "candidate"),
        "candidate_data_device": 64_769,
        "candidate_data_inode": 55_451_027,
        "live_data_dir": str(temporary_root / "live"),
        "live_data_device": 64_769,
        "live_data_inode": 55_451_026,
        "network_name": f"{identity.project_name}_default",
        "network_id": "2" * 64,
        "network_endpoint_id": "3" * 64,
        "runtime_sha256": "4" * 64,
        "trusted_proxy_cidrs": ["192.0.2.1/32"],
        "deployment_assets": deployment_asset_payload(product_root),
    }


def stub_state_candidate_ready_dependencies(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    product_root: Path,
    temporary_root: Path,
    current_candidate_identity: tuple[int, int, int, int] | None = None,
    current_live_identity: tuple[int, int, int, int] | None = None,
    current_network_id: str = "2" * 64,
    current_endpoint_id: str = "3" * 64,
    current_runtime_sha256: str = "4" * 64,
) -> object:
    identity = module.candidate_identity(VALID_SHA)
    expected_candidate_identity = (
        64_769,
        55_451_027,
        module.RUNTIME_UID,
        module.RUNTIME_GID,
    )
    expected_live_identity = (
        64_769,
        55_451_026,
        module.RUNTIME_UID,
        module.RUNTIME_GID,
    )
    data_identities = {
        "candidate": current_candidate_identity or expected_candidate_identity,
        "live": current_live_identity or expected_live_identity,
    }

    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(
        module,
        "validate_data_directories",
        lambda **_: (temporary_root / "live", temporary_root / "candidate"),
    )
    monkeypatch.setattr(module, "validate_deployment_layout", lambda **_: None)
    monkeypatch.setattr(
        module, "_data_directory_identity", lambda path: data_identities[path.name]
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
            network_id=current_network_id,
            subnet="192.0.2.0/24",
            gateway="192.0.2.1",
            endpoint_id=current_endpoint_id,
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
        lambda *_args, **_kwargs: current_runtime_sha256,
    )

    class NetworkRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            assert arguments == [
                module.DOCKER_BINARY,
                "network",
                "inspect",
                f"{identity.project_name}_default",
            ]
            return subprocess.CompletedProcess(arguments, 0, "[]", "")

    return NetworkRunner()


def load_deploy_tool() -> ModuleType:
    assert DEPLOY_TOOL.is_file(), "the workstation deploy tool must be versioned"
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_deploy", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_workstation_compose_has_hard_runtime_resource_and_log_limits() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert re.search(r"(?m)^\s+cpus:\s*[\"']?1\.0[\"']?\s*$", compose)
    assert re.search(r"(?m)^\s+mem_limit:\s*[\"']?512m[\"']?\s*$", compose)
    assert re.search(r"(?m)^\s+pids_limit:\s*128\s*$", compose)
    assert re.search(r"(?m)^\s+restart:\s*unless-stopped\s*$", compose)
    assert re.search(r"(?m)^\s+read_only:\s*true\s*$", compose)
    assert "no-new-privileges:true" in compose
    assert re.search(r"(?m)^\s+-\s*ALL\s*$", compose)
    assert re.search(r"(?m)^\s+driver:\s*local\s*$", compose)
    assert re.search(r"(?m)^\s+max-size:\s*[\"\']10m[\"\']\s*$", compose)
    assert re.search(r"(?m)^\s+max-file:\s*[\"\']3[\"\']\s*$", compose)


def test_main_static_gate_covers_the_workstation_deploy_tool() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "ruff check backend scripts deploy/workstation/deploy.py" in makefile
    assert (
        "ruff format --check backend scripts deploy/workstation/deploy.py" in makefile
    )
    assert (
        "mypy --config-file backend/pyproject.toml deploy/workstation/deploy.py"
        in makefile
    )


def test_workstation_compose_uses_an_immutable_image_and_private_state_boundary() -> (
    None
):
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "image: commerce-ops-desk:${SOURCE_SHA:?" in compose
    assert "127.0.0.1:${HOST_PORT:?host port required}:8000" in compose
    assert re.search(
        r"(?ms)^\s+volumes:\n"
        r"\s+- type: bind\n"
        r'\s+source: ["\']\$\{DATA_DIR:\?data directory required\}["\']\n'
        r"\s+target: /app/data\n"
        r"\s+bind:\n"
        r"\s+create_host_path: false$",
        compose,
    )
    assert compose.count("target: /app/data") == 1
    assert "/tmp:" in compose
    assert re.search(r"(?m)^\s+pull_policy:\s*never\s*$", compose)
    assert (
        'COMMERCE_OPS_ALLOWED_HOSTS: \'["commerce-ops-desk.srrsh.aig.rest",' in compose
    )
    assert "COMMERCE_OPS_TRUSTED_PROXY_CIDRS" in compose
    assert "access_code" not in compose.lower()
    assert "COMMERCE_OPS_SESSION_SECRET" not in compose
    assert "COMMERCE_OPS_WEBHOOK_MASTER_SECRET" not in compose


def test_workstation_caddy_template_enforces_the_public_request_boundary() -> None:
    caddy = CADDY_TEMPLATE.read_text(encoding="utf-8")

    assert "commerce-ops-desk.srrsh.aig.rest" in caddy
    assert "max_size 16KiB" in caddy
    assert re.search(
        r"path\s+/docs\s+/docs/\*\s+/redoc\s+/redoc/\*\s+/openapi\.json", caddy
    )
    assert "respond @api_docs 404" in caddy
    assert "reverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}" in caddy
    assert "header_up Host {http.request.host}" in caddy
    assert "header_up X-Forwarded-For {http.request.remote.host}" in caddy
    assert 'X-Content-Type-Options "nosniff"' in caddy
    assert 'X-Frame-Options "DENY"' in caddy
    assert "Content-Security-Policy" in caddy
    assert "Strict-Transport-Security" in caddy
    assert "includeSubDomains" not in caddy
    assert "rate_limit" not in caddy
    assert "access_code" not in caddy.lower()
    assert caddy.count("{{UPSTREAM_PORT}}") == 1
    assert caddy.count("{{ROUTE_REVISION}}") == 1
    assert 'X-CommerceOps-Route-Revision "{{ROUTE_REVISION}}"' in caddy
    assert "header_down -X-CommerceOps-Route-Revision" in caddy


@pytest.mark.parametrize(
    "invalid_sha",
    [
        "",
        "abc123",
        "A" * 40,
        "g" * 40,
        "a" * 39,
        "a" * 41,
        "a" * 64,
        " a" * 20,
    ],
)
def test_deploy_tool_requires_one_full_lowercase_git_sha(invalid_sha: str) -> None:
    module = load_deploy_tool()

    with pytest.raises(module.DeploymentError, match="40-character lowercase"):
        module.validate_source_sha(invalid_sha)


def test_deploy_tool_derives_unique_candidate_identity_from_the_sha() -> None:
    module = load_deploy_tool()

    identity = module.candidate_identity(VALID_SHA)

    assert identity.project_name == "commerce-ops-candidate-0123456789ab"
    assert identity.container_name == "app-commerce-ops-desk-candidate-0123456789ab"
    assert identity.image == f"commerce-ops-desk:{VALID_SHA}"
    assert identity.data_directory_name == "data-candidate-0123456789ab"


def test_deploy_docker_commands_use_the_fixed_local_daemon_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    monkeypatch.setenv("PATH", f"{tmp_path}/attacker-bin")
    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "attacker")
    monkeypatch.setenv("COMPOSE_FILE", "/tmp/attacker.yaml")
    observed: dict[str, object] = {}

    def fake_run(
        arguments: list[str],
        *,
        env: dict[str, str] | None,
        input: str | None,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        observed.update(arguments=arguments, environment=env, input=input)
        assert capture_output is True
        assert text is True
        assert check is False
        assert timeout == module.COMMAND_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(arguments, 0, "[]\n", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.CommandRunner().run([module.DOCKER_BINARY, "image", "inspect", "demo"])

    assert observed["arguments"] == [
        "/usr/bin/docker",
        "image",
        "inspect",
        "demo",
    ]
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert environment["DOCKER_CONFIG"] == "/etc/docker"
    assert environment["PATH"] == "/usr/bin:/bin"
    assert "DOCKER_CONTEXT" not in environment
    assert not any(name.startswith("COMPOSE_") for name in environment)


def test_deploy_rejects_a_different_local_docker_daemon() -> None:
    module = load_deploy_tool()

    class DaemonRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            assert arguments == [
                "/usr/bin/docker",
                "info",
                "--format",
                "{{json .ID}}",
            ]
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps("different-daemon"), ""
            )

    with pytest.raises(module.DeploymentError, match="does not match"):
        module._assert_local_docker_daemon(DaemonRunner(), DOCKER_DAEMON_ID)


def test_deployment_system_tools_use_fixed_absolute_paths() -> None:
    module = load_deploy_tool()

    class FixedToolRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.calls.append(command)
            stdout = ""
            returncode = 0
            if command[1:] == ["test", "-L", str(module.CADDY_SITE)]:
                returncode = 1
            elif command[1:] == ["cat", str(module.CADDY_SITE)]:
                stdout = "managed fragment\n"
            elif command[1:] == [
                "adapt",
                "--adapter",
                "caddyfile",
                "--config",
                "/dev/stdin",
            ]:
                stdout = "{}\n"
            return subprocess.CompletedProcess(command, returncode, stdout, "")

    runner = FixedToolRunner()
    runner._caddy_mutation_fence_token = "a" * 64
    assert module._listening_ports(runner) == set()
    assert module._adapt_fragment(runner, "fragment") == {}
    assert module._read_current_site(runner) == "managed fragment\n"
    module._validate_caddy(runner)
    module._reload_caddy(runner)

    assert runner.calls[0][0] == "/usr/bin/ss"
    assert runner.calls[1][0] == "/usr/bin/caddy"
    assert runner.calls[2:5] == [
        ["/usr/bin/sudo", "test", "-f", str(module.CADDY_SITE)],
        ["/usr/bin/sudo", "test", "-L", str(module.CADDY_SITE)],
        ["/usr/bin/sudo", "cat", str(module.CADDY_SITE)],
    ]
    assert runner.calls[5][:2] == ["/usr/bin/sudo", "/usr/bin/caddy"]
    assert runner.calls[6][:4] == [
        "/usr/bin/sudo",
        "/usr/bin/python3",
        "-I",
        "-c",
    ]
    assert "/usr/bin/systemctl" in runner.calls[6][4]


def test_reload_caddy_uses_blocking_fenced_root_wrapper() -> None:
    module = load_deploy_tool()

    class CaptureRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict[str, object]]] = []
            self._caddy_mutation_fence_token = "a" * 64

        def run(
            self,
            arguments: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

    runner = CaptureRunner()
    module._reload_caddy(runner)

    assert len(runner.calls) == 1
    command, options = runner.calls[0]
    assert command[:4] == [
        module.SUDO_BINARY,
        module.PYTHON_BINARY,
        "-I",
        "-c",
    ]
    helper = command[4]
    compile(helper, "<caddy-reload-helper>", "exec")
    assert "fcntl.flock" in helper
    assert "fence token" in helper
    assert "subprocess.run" in helper
    assert module.SYSTEMCTL_BINARY in helper
    assert "os.exec" not in helper
    assert command[-3:] == [
        "a" * 64,
        str(module.CADDY_MUTATION_LOCK),
        str(module.CADDY_MUTATION_FENCE),
    ]
    assert options["timeout"] is None


def test_compose_commands_use_verified_stdin_and_one_explicit_service() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)

    assert module._compose_command(
        identity,
        "create",
        "--no-build",
        module.COMPOSE_SERVICE,
    ) == [
        "/usr/bin/docker",
        "compose",
        "--project-name",
        identity.project_name,
        "--file",
        "-",
        "create",
        "--no-build",
        "commerce-ops-desk",
    ]


def test_compose_config_rejects_any_service_besides_the_managed_app() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)

    class ServiceRunner:
        def run(
            self,
            arguments: list[str],
            *,
            environment: dict[str, str],
            input_text: str,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            assert arguments == module._compose_command(
                identity, "config", "--services"
            )
            assert environment["SOURCE_SHA"] == VALID_SHA
            assert input_text == "verified compose bytes\n"
            return subprocess.CompletedProcess(
                arguments,
                0,
                "commerce-ops-desk\nprivileged-sidecar\n",
                "",
            )

    environment = module._compose_environment(
        identity,
        port=18088,
        data_directory=Path(
            "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab"
        ),
        trusted_proxy_json='["127.0.0.1/32"]',
    )
    with pytest.raises(module.DeploymentError, match="exactly one managed service"):
        module._assert_compose_service_contract(
            ServiceRunner(),
            identity,
            compose_yaml="verified compose bytes\n",
            environment=environment,
        )


def test_candidate_project_rejects_an_extra_sidecar_container() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    first_id = "1" * 64
    second_id = "2" * 64

    class ProjectRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            assert arguments == [
                "/usr/bin/docker",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={identity.project_name}",
                "--format",
                "{{.ID}}",
            ]
            return subprocess.CompletedProcess(
                arguments, 0, f"{first_id}\n{second_id}\n", ""
            )

    with pytest.raises(module.DeploymentError, match="exactly one container"):
        module._assert_exact_project_container(ProjectRunner(), identity)


def test_candidate_project_binds_container_id_name_and_service() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    container_id = "1" * 64

    class ProjectRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            if arguments[1:3] == ["container", "ls"]:
                return subprocess.CompletedProcess(
                    arguments, 0, f"{container_id}\n", ""
                )
            assert arguments == [
                "/usr/bin/docker",
                "container",
                "inspect",
                container_id,
            ]
            document = [
                {
                    "Id": container_id,
                    "Name": f"/{identity.container_name}",
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": identity.project_name,
                            "com.docker.compose.service": "privileged-sidecar",
                        }
                    },
                }
            ]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(document), "")

    with pytest.raises(module.DeploymentError, match="identity is invalid"):
        module._assert_exact_project_container(ProjectRunner(), identity)


def test_candidate_network_rejects_any_endpoint_besides_the_final_container() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    network_name = f"{identity.project_name}_default"
    container_id = "1" * 64
    sidecar_id = "2" * 64
    inspection = [
        {
            "Name": network_name,
            "Id": "a" * 64,
            "Created": "2026-10-09T00:00:00.000000000Z",
            "Scope": "local",
            "Driver": "bridge",
            "EnableIPv4": True,
            "EnableIPv6": False,
            "Labels": {
                "com.docker.compose.config-hash": "d" * 64,
                "com.docker.compose.project": identity.project_name,
                "com.docker.compose.network": "default",
                "com.docker.compose.version": "2.40.3",
            },
            "IPAM": {
                "Driver": "default",
                "Options": None,
                "Config": [{"Gateway": "192.0.2.1", "Subnet": "192.0.2.0/24"}],
            },
            "Internal": False,
            "Attachable": False,
            "Ingress": False,
            "ConfigFrom": {"Network": ""},
            "ConfigOnly": False,
            "Containers": {
                container_id: {
                    "Name": identity.container_name,
                    "EndpointID": "b" * 64,
                    "MacAddress": "02:42:c0:00:02:02",
                    "IPv4Address": "192.0.2.2/24",
                    "IPv6Address": "",
                },
                sidecar_id: {
                    "Name": "privileged-sidecar",
                    "EndpointID": "c" * 64,
                    "MacAddress": "02:42:c0:00:02:03",
                    "IPv4Address": "192.0.2.3/24",
                    "IPv6Address": "",
                },
            },
            "Options": {},
        }
    ]

    with pytest.raises(module.DeploymentError, match="network endpoint set"):
        module._assert_candidate_network(
            identity,
            network_name,
            inspection,
            expected_container_id=container_id,
        )


@pytest.mark.parametrize(
    "invalid_port",
    ["", "0", "08088", "65536", "-1", "18088.0", " 18088", "18088 ", "abc"],
)
def test_deploy_tool_requires_a_canonical_numeric_candidate_port(
    invalid_port: str,
) -> None:
    module = load_deploy_tool()

    with pytest.raises(module.DeploymentError, match="candidate port"):
        module.validate_candidate_port(invalid_port)


def test_deploy_tool_accepts_strict_independent_data_directories(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    live = app_root / "data-live"
    candidate = app_root / "data-candidate-0123456789ab"
    live.mkdir(parents=True)
    candidate.mkdir()
    live.chmod(0o700)
    candidate.chmod(0o700)

    validated = module.validate_data_directories(
        app_root=app_root,
        live_data_dir=live,
        candidate_data_dir=candidate,
        source_sha=VALID_SHA,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    assert validated == (live, candidate)


def test_deployment_layout_rejects_state_or_data_inside_the_code_repository(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    code_root = app_root / "code"
    state_directory = app_root / "deploy-state"
    live_data = code_root / "data-live"
    candidate_data = app_root / "data-candidate-0123456789ab"
    for directory in (code_root, state_directory, live_data, candidate_data):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    with pytest.raises(module.DeploymentError, match="must be outside the code"):
        module.validate_deployment_layout(
            product_root=code_root,
            app_root=app_root,
            state_directory=state_directory,
            live_data_dir=live_data,
            candidate_data_dir=candidate_data,
        )


def test_deploy_tool_rejects_candidate_data_outside_the_app_root(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    live = app_root / "data-live"
    candidate = tmp_path / "data-candidate-0123456789ab"
    live.mkdir(parents=True)
    candidate.mkdir()
    live.chmod(0o700)
    candidate.chmod(0o700)

    with pytest.raises(module.DeploymentError, match="inside"):
        module.validate_data_directories(
            app_root=app_root,
            live_data_dir=live,
            candidate_data_dir=candidate,
            source_sha=VALID_SHA,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )


def test_deploy_tool_rejects_symlinked_or_permissive_data_directories(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    live_target = app_root / "live-target"
    live = app_root / "data-live"
    candidate = app_root / "data-candidate-0123456789ab"
    live_target.mkdir(parents=True)
    live.symlink_to(live_target, target_is_directory=True)
    candidate.mkdir()
    candidate.chmod(0o755)

    with pytest.raises(module.DeploymentError, match="symbolic link"):
        module.validate_data_directories(
            app_root=app_root,
            live_data_dir=live,
            candidate_data_dir=candidate,
            source_sha=VALID_SHA,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    live.unlink()
    live.mkdir()
    live.chmod(0o700)
    with pytest.raises(module.DeploymentError, match="0700"):
        module.validate_data_directories(
            app_root=app_root,
            live_data_dir=live,
            candidate_data_dir=candidate,
            source_sha=VALID_SHA,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )


@pytest.mark.parametrize("candidate_shape", ["hidden", "nested", "symbolic-link"])
def test_deploy_tool_rejects_noncanonical_candidate_directory_shapes(
    tmp_path: Path, candidate_shape: str
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    live = app_root / "data-live"
    live.mkdir(parents=True)
    live.chmod(0o700)
    expected = app_root / "data-candidate-0123456789ab"
    if candidate_shape == "hidden":
        candidate = app_root / ".data-candidate-0123456789ab"
        candidate.mkdir()
    elif candidate_shape == "nested":
        candidate = app_root / "archive" / "data-candidate-0123456789ab"
        candidate.mkdir(parents=True)
    else:
        target = app_root / "candidate-target"
        target.mkdir()
        target.chmod(0o700)
        expected.symlink_to(target, target_is_directory=True)
        candidate = expected
    if not candidate.is_symlink():
        candidate.chmod(0o700)

    with pytest.raises(module.DeploymentError, match="exactly|symbolic link"):
        module.validate_data_directories(
            app_root=app_root,
            live_data_dir=live,
            candidate_data_dir=candidate,
            source_sha=VALID_SHA,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )


def test_initial_candidate_empty_check_uses_an_isolated_privileged_helper(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    identity = module._data_directory_identity(candidate)

    class CaptureRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], frozenset[int]]] = []

        def run(
            self,
            arguments: list[str],
            *,
            allowed_returncodes: frozenset[int] = frozenset({0}),
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((list(arguments), allowed_returncodes))
            return subprocess.CompletedProcess(arguments, 0, "", "")

    runner = CaptureRunner()
    module._assert_initial_candidate_data(runner, candidate, identity)

    assert len(runner.calls) == 1
    command, allowed_returncodes = runner.calls[0]
    assert command[:5] == [
        module.SUDO_BINARY,
        module.PYTHON_BINARY,
        "-I",
        "-c",
        module.CANDIDATE_DATA_FRESHNESS_HELPER,
    ]
    compile(command[4], "<candidate-data-freshness-helper>", "exec")
    assert command[5:] == [str(candidate), *(str(value) for value in identity)]
    assert allowed_returncodes == frozenset({0, 3})


@pytest.mark.parametrize(
    ("candidate_entry", "expected_returncode"),
    [
        ("empty", 0),
        ("regular-file", 3),
        ("hidden-file", 3),
        ("subdirectory", 3),
        ("symbolic-link", 3),
    ],
)
def test_isolated_candidate_freshness_helper_detects_every_entry_type(
    tmp_path: Path,
    candidate_entry: str,
    expected_returncode: int,
) -> None:
    module = load_deploy_tool()
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    if candidate_entry == "regular-file":
        (candidate / "state.sqlite3").write_text("state\n", encoding="utf-8")
    elif candidate_entry == "hidden-file":
        (candidate / ".state").write_text("state\n", encoding="utf-8")
    elif candidate_entry == "subdirectory":
        (candidate / "nested").mkdir()
    elif candidate_entry == "symbolic-link":
        (candidate / "state-link").symlink_to(tmp_path / "missing-state")
    identity = module._data_directory_identity(candidate)

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            module.CANDIDATE_DATA_FRESHNESS_HELPER,
            str(candidate),
            *(str(value) for value in identity),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    assert result.stdout == ""
    assert result.stderr == ""


def test_deploy_tool_derives_one_ipv4_32_from_the_created_network() -> None:
    module = load_deploy_tool()
    inspection = [
        {
            "Name": "commerce-ops-candidate-0123456789ab_default",
            "IPAM": {
                "Config": [
                    {"Subnet": "192.0.2.0/24", "Gateway": "192.0.2.1"},
                ]
            },
        }
    ]

    assert module.trusted_proxy_json_from_network_inspection(inspection) == (
        '["192.0.2.1/32"]'
    )


@pytest.mark.parametrize(
    "inspection",
    [
        [],
        [{"IPAM": {"Config": []}}],
        [
            {
                "IPAM": {
                    "Config": [
                        {"Gateway": "192.0.2.1"},
                        {"Gateway": "198.51.100.1"},
                    ]
                }
            }
        ],
        [{"IPAM": {"Config": [{"Gateway": "2001:db8::1"}]}}],
        [{"IPAM": {"Config": [{"Gateway": "not-an-ip"}]}}],
    ],
)
def test_deploy_tool_rejects_ambiguous_or_non_ipv4_proxy_networks(
    inspection: object,
) -> None:
    module = load_deploy_tool()

    with pytest.raises(module.DeploymentError, match="one IPv4 gateway"):
        module.trusted_proxy_json_from_network_inspection(inspection)


def test_deploy_tool_detects_listener_docker_and_caddy_port_claims() -> None:
    module = load_deploy_tool()
    docker_inspection = [
        {
            "Name": "/another-app",
            "HostConfig": {
                "PortBindings": {
                    "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18088"}]
                }
            },
        }
    ]

    with pytest.raises(module.DeploymentError, match="listener"):
        module.assert_port_unclaimed(
            18088,
            listening_ports={18088},
            docker_inspections=[],
            caddy_documents={},
        )
    with pytest.raises(module.DeploymentError, match="another-app"):
        module.assert_port_unclaimed(
            18088,
            listening_ports=set(),
            docker_inspections=docker_inspection,
            caddy_documents={},
        )
    with pytest.raises(module.DeploymentError, match="other.conf"):
        module.assert_port_unclaimed(
            18088,
            listening_ports=set(),
            docker_inspections=[],
            caddy_documents={"other.conf": "reverse_proxy 127.0.0.1:18088"},
        )


def test_candidate_tag_must_still_match_the_verified_manifest_image_id() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)

    class ImageRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            assert command == [
                "/usr/bin/docker",
                "image",
                "inspect",
                identity.image,
            ]
            inspection = [
                {
                    "Id": "sha256:" + "2" * 64,
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.revision": VALID_SHA,
                        }
                    },
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(inspection), "")

    with pytest.raises(module.DeploymentError, match="manifest"):
        module._assert_image_revision(
            ImageRunner(),
            identity,
            expected_image_id="sha256:" + "1" * 64,
        )


def _valid_image_config() -> dict[str, object]:
    return {
        "Cmd": ["bash", "scripts/start-hosted.sh"],
        "Env": [
            "COMMERCE_OPS_DATABASE_URL=sqlite+pysqlite:////app/data/commerce_ops.db",
            "COMMERCE_OPS_ENVIRONMENT=demo",
            f"COMMERCE_OPS_SOURCE_SHA={VALID_SHA}",
            "COMMERCE_OPS_VENV_DIR=/opt/venv",
            "PORT=8000",
        ],
        "ExposedPorts": {"8000/tcp": {}},
        "Healthcheck": {"Test": ["CMD", "health-probe"]},
        "User": "10001:10001",
        "Volumes": {"/app/data": {}},
        "WorkingDir": "/app",
    }


def test_immutable_candidate_image_rejects_mismatched_runtime_source_sha() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    image_id = "sha256:" + "1" * 64
    config = _valid_image_config()
    config["Labels"] = {"org.opencontainers.image.revision": VALID_SHA}
    environment = config["Env"]
    assert isinstance(environment, list)
    environment[:] = [
        f"COMMERCE_OPS_SOURCE_SHA={'f' * 40}"
        if entry.startswith("COMMERCE_OPS_SOURCE_SHA=")
        else entry
        for entry in environment
    ]

    class ImageRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            assert command == ["/usr/bin/docker", "image", "inspect", image_id]
            inspection = [{"Id": image_id, "Config": config}]
            return subprocess.CompletedProcess(command, 0, json.dumps(inspection), "")

    with pytest.raises(module.DeploymentError, match="runtime source SHA"):
        module._immutable_image_config(ImageRunner(), identity, image_id)


@pytest.mark.parametrize(
    "source_entries",
    [
        [],
        [
            f"COMMERCE_OPS_SOURCE_SHA={VALID_SHA}",
            f"COMMERCE_OPS_SOURCE_SHA={VALID_SHA}",
        ],
    ],
)
def test_immutable_candidate_image_requires_exactly_one_runtime_source_sha(
    source_entries: list[str],
) -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    image_id = "sha256:" + "1" * 64
    config = _valid_image_config()
    config["Labels"] = {"org.opencontainers.image.revision": VALID_SHA}
    environment = config["Env"]
    assert isinstance(environment, list)
    config["Env"] = [
        entry
        for entry in environment
        if not entry.startswith("COMMERCE_OPS_SOURCE_SHA=")
    ] + source_entries

    class ImageRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            inspection = [{"Id": image_id, "Config": config}]
            return subprocess.CompletedProcess(command, 0, json.dumps(inspection), "")

    with pytest.raises(module.DeploymentError, match="exactly one runtime source SHA"):
        module._immutable_image_config(ImageRunner(), identity, image_id)


def _valid_candidate_inspection(module: ModuleType) -> dict[str, object]:
    identity = module.candidate_identity(VALID_SHA)
    return {
        "Image": "sha256:" + "1" * 64,
        "Config": {
            "Image": identity.image,
            "User": "10001:10001",
            "Cmd": ["bash", "scripts/start-hosted.sh"],
            "Entrypoint": None,
            "ExposedPorts": {"8000/tcp": {}},
            "Healthcheck": {"Test": ["CMD", "health-probe"]},
            "Volumes": {"/app/data": {}},
            "WorkingDir": "/app",
            "Labels": {
                "org.opencontainers.image.revision": VALID_SHA,
                "com.docker.compose.project": identity.project_name,
                "com.docker.compose.service": "commerce-ops-desk",
            },
            "Env": [
                "COMMERCE_OPS_DATABASE_URL=sqlite+pysqlite:////app/data/commerce_ops.db",
                "COMMERCE_OPS_ENVIRONMENT=demo",
                f"COMMERCE_OPS_SOURCE_SHA={VALID_SHA}",
                "COMMERCE_OPS_VENV_DIR=/opt/venv",
                'COMMERCE_OPS_ALLOWED_HOSTS=["commerce-ops-desk.srrsh.aig.rest","127.0.0.1","localhost"]',
                'COMMERCE_OPS_TRUSTED_PROXY_CIDRS=["192.0.2.1/32"]',
                "COMMERCE_OPS_COOKIE_SECURE=true",
                "COMMERCE_OPS_WEBHOOK_ENABLED=true",
                "PORT=8000",
            ],
        },
        "HostConfig": {
            "AutoRemove": False,
            "Binds": None,
            "BlkioWeight": 0,
            "CgroupParent": "",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "Devices": None,
            "DeviceCgroupRules": None,
            "DeviceRequests": None,
            "Dns": None,
            "DnsOptions": None,
            "DnsSearch": None,
            "ExtraHosts": [],
            "GroupAdd": None,
            "Links": None,
            "VolumesFrom": None,
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "NetworkMode": f"{identity.project_name}_default",
            "SecurityOpt": ["no-new-privileges:true"],
            "Init": True,
            "RestartPolicy": {
                "Name": "unless-stopped",
                "MaximumRetryCount": 0,
            },
            "PortBindings": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18088"}]
            },
            "Tmpfs": {
                "/tmp": (
                    "rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=1777"
                )
            },
            "NanoCpus": 1_000_000_000,
            "Memory": 512 * 1024 * 1024,
            "MemoryReservation": 0,
            "MemorySwap": 1024 * 1024 * 1024,
            "MemorySwappiness": None,
            "OomKillDisable": False,
            "PidsLimit": 128,
            "PublishAllPorts": False,
            "Runtime": "runc",
            "ShmSize": 64 * 1024 * 1024,
            "Sysctls": None,
            "Ulimits": None,
            "VolumeDriver": "",
            "LogConfig": {
                "Type": "local",
                "Config": {"max-size": "10m", "max-file": "3"},
            },
            "MaskedPaths": sorted(module.REQUIRED_MASKED_PATHS),
            "ReadonlyPaths": sorted(module.REQUIRED_READONLY_PATHS),
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab",
                "Destination": "/app/data",
                "Mode": "",
                "Propagation": "rprivate",
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Networks": {f"{identity.project_name}_default": {"Gateway": "192.0.2.1"}}
        },
    }


@pytest.mark.parametrize(
    ("field", "replacement", "error_match"),
    [
        (("Image",), "sha256:" + "2" * 64, "image ID"),
        (("Config", "Image"), "commerce-ops-desk:wrong", "image reference"),
        (
            ("Config", "Labels", "org.opencontainers.image.revision"),
            "f" * 40,
            "revision",
        ),
        (("Config", "Labels", "com.docker.compose.project"), "wrong", "project"),
        (("Config", "Labels", "com.docker.compose.service"), "wrong", "service"),
        (
            ("NetworkSettings", "Networks"),
            {"wrong_default": {"Gateway": "192.0.2.1"}},
            "network",
        ),
        (
            (
                "NetworkSettings",
                "Networks",
                "commerce-ops-candidate-0123456789ab_default",
                "Gateway",
            ),
            "192.0.2.254",
            "gateway",
        ),
    ],
)
def test_candidate_runtime_is_bound_to_the_exact_image_compose_project_and_network(
    field: tuple[str, ...], replacement: object, error_match: str
) -> None:
    module = load_deploy_tool()
    inspection = _valid_candidate_inspection(module)
    target: dict[str, object] = inspection
    for key in field[:-1]:
        child = target[key]
        assert isinstance(child, dict)
        target = child
    target[field[-1]] = replacement

    identity = module.candidate_identity(VALID_SHA)
    with pytest.raises(module.DeploymentError, match=error_match):
        module.validate_candidate_runtime_inspection(
            inspection,
            identity=identity,
            expected_image_id="sha256:" + "1" * 64,
            network_name=f"{identity.project_name}_default",
            port=18088,
            data_directory=Path(
                "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab"
            ),
            trusted_proxy_json='["192.0.2.1/32"]',
            image_config=_valid_image_config(),
        )


def test_candidate_runtime_accepts_the_exact_prepared_identity() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)

    module.validate_candidate_runtime_inspection(
        _valid_candidate_inspection(module),
        identity=identity,
        expected_image_id="sha256:" + "1" * 64,
        network_name=f"{identity.project_name}_default",
        port=18088,
        data_directory=Path(
            "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab"
        ),
        trusted_proxy_json='["192.0.2.1/32"]',
        image_config=_valid_image_config(),
    )


def test_atomic_site_install_passes_only_digest_bound_bytes_to_the_root_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    monkeypatch.setattr(module, "CADDY_SITE", tmp_path / "site.conf")
    content = "candidate\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    current_digest = hashlib.sha256(b"current\n").hexdigest()

    class CaptureRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.inputs: list[str | None] = []

        def run(
            self,
            arguments: list[str],
            *,
            input_text: str | None = None,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.commands.append(command)
            self.inputs.append(input_text)
            return subprocess.CompletedProcess(command, 0, "", "")

    runner = CaptureRunner()
    runner._caddy_mutation_fence_token = "a" * 64
    module._atomic_install_site(
        runner,
        content,
        expected_sha256=digest,
        expected_current_sha256=current_digest,
    )

    assert len(runner.commands) == 1
    assert runner.commands[0][:4] == [
        module.SUDO_BINARY,
        module.PYTHON_BINARY,
        "-I",
        "-c",
    ]
    helper = runner.commands[0][4]
    compile(helper, "<atomic-site-helper>", "exec")
    assert "fcntl.flock" in helper
    assert "fence token" in helper
    assert "current Caddy site digest" in helper
    assert runner.commands[0][-2:] == [
        str(module.CADDY_MUTATION_LOCK),
        str(module.CADDY_MUTATION_FENCE),
    ]
    assert current_digest in runner.commands[0]
    assert runner.inputs == [content]


def test_atomic_site_install_rejects_unbound_bytes_before_invoking_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    monkeypatch.setattr(module, "CADDY_SITE", tmp_path / "site.conf")

    class UnexpectedRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"root helper unexpectedly invoked: {arguments}")

    with pytest.raises(module.DeploymentError, match="digest"):
        module._atomic_install_site(
            UnexpectedRunner(),
            "candidate\n",
            expected_sha256="0" * 64,
            expected_current_sha256="1" * 64,
        )


def test_build_manifest_loads_only_a_private_strict_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    manifest_path = state_directory / "verified-build.json"
    payload = {
        "approved_remote_ref": "refs/remotes/origin/main",
        "deployment_assets": deployment_asset_payload(),
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "image_id": "sha256:" + "1" * 64,
        "image_reference": f"commerce-ops-desk:{VALID_SHA}",
        "schema": 2,
        "source_sha": VALID_SHA,
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_path.chmod(0o600)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)

    assert module._load_build_manifest(manifest_path) == payload


def test_verified_deployment_assets_are_read_once_into_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    original = write_deployment_assets(product_root)
    identity = deployment_asset_payload(product_root)
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)

    assets = module._load_verified_deployment_assets(identity)
    for relative_path in DEPLOYMENT_ASSET_PATHS:
        (product_root / relative_path).write_text("changed after verification\n")

    assert assets.compose_yaml == original[DEPLOYMENT_ASSET_PATHS[0]]
    assert assets.caddy_templates == {
        module.CADDY_PROFILE_HARDENED: original[DEPLOYMENT_ASSET_PATHS[1]],
        module.CADDY_PROFILE_LEGACY_V020: original[DEPLOYMENT_ASSET_PATHS[2]],
    }


def test_prepare_rejects_post_build_compose_sidecar_before_any_docker_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    write_deployment_assets(product_root)
    asset_identity = deployment_asset_payload(product_root)
    (product_root / DEPLOYMENT_ASSET_PATHS[0]).write_text(
        "services:\n"
        "  commerce-ops-desk:\n"
        "    image: demo\n"
        "  privileged-sidecar:\n"
        "    image: attacker\n"
        "    privileged: true\n",
        encoding="utf-8",
    )
    manifest = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "approved_remote_ref": "refs/remotes/origin/main",
        "image_reference": f"commerce-ops-desk:{VALID_SHA}",
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "deployment_assets": asset_identity,
    }
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "_load_build_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        module,
        "validate_data_directories",
        lambda **_: (tmp_path / "live", tmp_path / "candidate"),
    )
    monkeypatch.setattr(module, "validate_deployment_layout", lambda **_: None)

    class NoDockerRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            pytest.fail(f"Docker must not run after asset drift: {arguments}")

    arguments = argparse.Namespace(
        build_manifest=str(tmp_path / "manifest.json"),
        candidate_port="18088",
        live_data_dir=str(tmp_path / "live"),
        candidate_data_dir=str(tmp_path / "candidate"),
    )
    with pytest.raises(module.DeploymentError, match="digest changed"):
        module.prepare_candidate(arguments, NoDockerRunner())


def test_prepare_rejects_a_historical_empty_candidate_directory_before_sudo_or_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    product_root = app_root / "code"
    state_directory = app_root / "deploy-state"
    live = app_root / "data-live"
    candidate = app_root / "data-candidate-0123456789ab"
    write_deployment_assets(product_root)
    for directory in (state_directory, live, candidate):
        directory.mkdir(mode=0o700, parents=True)
    manifest = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "approved_remote_ref": "refs/remotes/origin/main",
        "image_reference": f"commerce-ops-desk:{VALID_SHA}",
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "deployment_assets": deployment_asset_payload(product_root),
    }
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "_load_build_manifest", lambda _path: manifest)

    class NoCommandRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            pytest.fail(f"no command may run for a historical candidate: {arguments}")

    arguments = argparse.Namespace(
        build_manifest=str(state_directory / "manifest.json"),
        candidate_port="18088",
        live_data_dir=str(live),
        candidate_data_dir=str(candidate),
    )

    with pytest.raises(module.DeploymentError, match="must not already exist"):
        module.prepare_candidate(arguments, NoCommandRunner())


def test_candidate_directory_creation_uses_one_fixed_isolated_privileged_helper(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    identity = module.candidate_identity(VALID_SHA)

    class CaptureRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(arguments))
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"64769|55451027|{module.RUNTIME_UID}|{module.RUNTIME_GID}\n",
                "",
            )

    runner = CaptureRunner()
    candidate_path, candidate_identity = module._create_candidate_data_directory(
        runner,
        app_root=app_root,
        identity=identity,
    )

    assert candidate_path == app_root / identity.data_directory_name
    assert candidate_identity == (
        64_769,
        55_451_027,
        module.RUNTIME_UID,
        module.RUNTIME_GID,
    )
    assert len(runner.calls) == 1
    assert runner.calls[0][:5] == [
        module.SUDO_BINARY,
        module.PYTHON_BINARY,
        "-I",
        "-c",
        module.CANDIDATE_DATA_CREATE_HELPER,
    ]
    compile(runner.calls[0][4], "<candidate-data-create-helper>", "exec")
    assert runner.calls[0][5:] == [
        str(app_root),
        identity.data_directory_name,
        str(os.geteuid()),
        str(os.getegid()),
        str(module.RUNTIME_UID),
        str(module.RUNTIME_GID),
    ]


@pytest.mark.parametrize("root_violation", ["owner-uid", "owner-gid", "mode"])
def test_candidate_directory_creation_rejects_an_unsafe_application_root_before_sudo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_violation: str,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    identity = module.candidate_identity(VALID_SHA)
    actual_uid = os.geteuid()
    actual_gid = os.getegid()
    if root_violation == "owner-uid":
        monkeypatch.setattr(module.os, "geteuid", lambda: actual_uid + 1)
    elif root_violation == "owner-gid":
        monkeypatch.setattr(module.os, "getegid", lambda: actual_gid + 1)
    else:
        app_root.chmod(0o755)

    class CaptureRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(arguments))
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"64769|55451027|{module.RUNTIME_UID}|{module.RUNTIME_GID}\n",
                "",
            )

    runner = CaptureRunner()
    with pytest.raises(module.DeploymentError, match="application root"):
        module._create_candidate_data_directory(
            runner,
            app_root=app_root,
            identity=identity,
        )

    assert runner.calls == []


def test_isolated_candidate_creation_helper_atomically_creates_and_binds_one_inode(
    rename_noreplace_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = rename_noreplace_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    identity = module.candidate_identity(VALID_SHA)
    command = [
        sys.executable,
        "-I",
        "-c",
        module.CANDIDATE_DATA_CREATE_HELPER,
        str(app_root),
        identity.data_directory_name,
        str(os.geteuid()),
        str(os.getegid()),
        str(os.geteuid()),
        str(os.getegid()),
    ]

    created = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    assert created.returncode == 0, created.stderr
    candidate = app_root / identity.data_directory_name
    metadata = candidate.lstat()
    assert created.stdout == (
        f"{metadata.st_dev}|{metadata.st_ino}|{metadata.st_uid}|{metadata.st_gid}\n"
    )
    assert metadata.st_uid == os.geteuid()
    assert metadata.st_gid == os.getegid()
    assert metadata.st_mode & 0o777 == 0o700
    assert not list(candidate.iterdir())

    repeated = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode != 0
    assert candidate.lstat().st_ino == metadata.st_ino


@pytest.mark.parametrize(
    "root_violation",
    ["owner-uid", "owner-gid", "mode"],
)
def test_isolated_candidate_creation_helper_rejects_unsafe_application_root_metadata(
    tmp_path: Path,
    root_violation: str,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    identity = module.candidate_identity(VALID_SHA)
    expected_uid = os.geteuid()
    expected_gid = os.getegid()
    if root_violation == "owner-uid":
        expected_uid += 1
    elif root_violation == "owner-gid":
        expected_gid += 1
    else:
        app_root.chmod(0o777)

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            module.CANDIDATE_DATA_CREATE_HELPER,
            str(app_root),
            identity.data_directory_name,
            str(expected_uid),
            str(expected_gid),
            str(os.geteuid()),
            str(os.getegid()),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unsafe application root" in result.stderr
    assert not os.path.lexists(app_root / identity.data_directory_name)
    assert list(app_root.iterdir()) == []


def test_candidate_creation_helper_does_not_publish_before_identity_configuration(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    identity = module.candidate_identity(VALID_SHA)
    interrupted_helper = module.CANDIDATE_DATA_CREATE_HELPER.replace(
        "import sys\n",
        """\
import sys

def interrupt_fchown(*_arguments):
    raise OSError("simulated interruption before identity configuration")

os.fchown = interrupt_fchown
""",
        1,
    )
    assert interrupted_helper != module.CANDIDATE_DATA_CREATE_HELPER

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            interrupted_helper,
            str(app_root),
            identity.data_directory_name,
            str(os.geteuid()),
            str(os.getegid()),
            str(os.geteuid()),
            str(os.getegid()),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not os.path.lexists(app_root / identity.data_directory_name)
    assert all(path.name.startswith(".") for path in app_root.iterdir())


def test_candidate_creation_helper_uses_hidden_staging_and_noreplace_publish() -> None:
    module = load_deploy_tool()
    helper = module.CANDIDATE_DATA_CREATE_HELPER

    for required_contract in (
        "staging_name",
        'f".{child_name}.',
        "renameat2",
        "RENAME_NOREPLACE",
    ):
        assert required_contract in helper
    assert "os.mkdir(child_name" not in helper
    publish_offset = helper.rindex("renameat2(")
    assert helper.index("os.fchown(") < publish_offset
    assert helper.index("os.fchmod(") < publish_offset
    assert helper.index("os.fsync(child_descriptor)") < publish_offset
    assert helper.index("os.fsync(root_descriptor)") < publish_offset


def test_candidate_creation_helper_never_replaces_a_prepositioned_final_path(
    rename_noreplace_path: Path,
) -> None:
    module = load_deploy_tool()
    app_root = rename_noreplace_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    identity = module.candidate_identity(VALID_SHA)
    candidate = app_root / identity.data_directory_name
    candidate.write_text("pre-positioned\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            module.CANDIDATE_DATA_CREATE_HELPER,
            str(app_root),
            identity.data_directory_name,
            str(os.geteuid()),
            str(os.getegid()),
            str(os.geteuid()),
            str(os.getegid()),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert candidate.read_text(encoding="utf-8") == "pre-positioned\n"


def test_candidate_prepare_lock_rejects_a_concurrent_prepare_for_the_same_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)

    with (
        module._candidate_prepare_lock(VALID_SHA),
        pytest.raises(module.DeploymentError, match="already in progress"),
        module._candidate_prepare_lock(VALID_SHA),
    ):
        pytest.fail("a concurrent prepare must not acquire the same SHA lock")

    lock_path = state_directory / f"candidate-{VALID_SHA[:12]}.prepare.lock"
    metadata = lock_path.lstat()
    assert metadata.st_uid == os.geteuid()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o777 == 0o600


def test_candidate_prepare_lock_reports_replacement_when_body_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    lock_path = state_directory / f"candidate-{VALID_SHA[:12]}.prepare.lock"

    with (
        pytest.raises(module.DeploymentError, match="lock changed") as error,
        module._candidate_prepare_lock(VALID_SHA),
    ):
        lock_path.unlink()
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o600)
        raise module.DeploymentError("body failed")

    original_error = error.value.__cause__ or error.value.__context__
    assert isinstance(original_error, module.DeploymentError)
    assert str(original_error) == "body failed"


@pytest.mark.parametrize("candidate_shape", ["data-only", "state-only", "both"])
def test_quarantine_candidate_inputs_moves_each_shape_under_the_prepare_lock(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_shape: str,
) -> None:
    module = load_deploy_tool()
    tmp_path = rename_noreplace_path
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    create_candidate_prepare_lock(app_root)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    if candidate_shape in {"data-only", "both"}:
        data_path.mkdir(mode=0o700)
        (data_path / ".retained").write_text("keep\n", encoding="utf-8")
    if candidate_shape in {"state-only", "both"}:
        state_path.write_text('{"keep": true}\n', encoding="utf-8")
        state_path.chmod(0o600)
    root_identity = quarantine_identity(app_root.stat())
    data_identity = (
        quarantine_identity(data_path.stat())
        if candidate_shape != "state-only"
        else None
    )
    local_moves: list[str] = []
    real_rename = module._rename_candidate_input_noreplace

    def record_local_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        if source == data_path.name:
            pytest.fail("runtime-owned candidate data must use the sudo helper")
        local_moves.append(source)
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        module,
        "_rename_candidate_input_noreplace",
        record_local_rename,
    )
    runner = InlineSudoPythonRunner(module)

    archive = module.quarantine_candidate_inputs(VALID_SHA, runner)

    assert archive.parent == app_root
    assert archive.name.startswith(f"quarantine-candidate-{VALID_SHA[:12]}-")
    assert archive.stat().st_mode & 0o777 == 0o700
    assert not data_path.exists()
    assert not state_path.exists()
    assert (archive / "data").exists() is (candidate_shape != "state-only")
    assert (archive / state_path.name).exists() is (candidate_shape != "data-only")
    if candidate_shape != "state-only":
        assert (archive / "data" / ".retained").read_text(encoding="utf-8") == (
            "keep\n"
        )
    assert len(runner.calls) == (candidate_shape != "state-only")
    if data_identity is not None:
        assert quarantine_identity((archive / "data").stat()) == data_identity
        assert runner.calls[0][5:] == [
            str(app_root),
            data_path.name,
            archive.name,
            *(str(value) for value in root_identity),
            *(str(value) for value in quarantine_identity(archive.stat())),
            *(str(value) for value in data_identity),
        ]
        assert runner.results[0].stdout == (
            "|".join(str(value) for value in data_identity) + "\n"
        )
    assert local_moves == ([state_path.name] if candidate_shape != "data-only" else [])


def test_quarantine_candidate_inputs_holds_prepare_lock_through_both_moves(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    tmp_path = rename_noreplace_path
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    create_candidate_prepare_lock(app_root)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    state_path.write_text('{"keep": true}\n', encoding="utf-8")
    state_path.chmod(0o600)
    real_rename = module._rename_candidate_input_noreplace
    observed_moves: list[str] = []

    def assert_lock_during_data_helper(_command: list[str]) -> None:
        with (
            pytest.raises(module.DeploymentError, match="already in progress"),
            module._candidate_prepare_lock(VALID_SHA),
        ):
            pytest.fail("quarantine released its SHA lock before the data helper")
        observed_moves.append(data_path.name)

    def assert_lock_then_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        with (
            pytest.raises(module.DeploymentError, match="already in progress"),
            module._candidate_prepare_lock(VALID_SHA),
        ):
            pytest.fail("quarantine released its SHA lock before moving both inputs")
        observed_moves.append(source)
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        module,
        "_rename_candidate_input_noreplace",
        assert_lock_then_rename,
    )

    runner = InlineSudoPythonRunner(
        module,
        before_execute=assert_lock_during_data_helper,
    )
    module.quarantine_candidate_inputs(VALID_SHA, runner)

    assert observed_moves == [data_path.name, state_path.name]
    assert len(runner.calls) == 1


def test_candidate_data_quarantine_helper_never_replaces_a_prepositioned_destination(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root, _state_directory, data_path, _state_path = (
        prepare_candidate_quarantine_inputs(
            module,
            rename_noreplace_path,
            monkeypatch,
        )
    )
    original_identity = quarantine_identity(data_path.stat())
    prepositioned: Path | None = None

    def insert_archive_destination(command: list[str]) -> None:
        nonlocal prepositioned
        assert len(command) == 20
        archive = app_root / command[7]
        prepositioned = archive / "data"
        prepositioned.write_text("pre-positioned\n", encoding="utf-8")

    runner = InlineSudoPythonRunner(
        module,
        before_execute=insert_archive_destination,
    )

    with pytest.raises(module.DeploymentError):
        module.quarantine_candidate_inputs(VALID_SHA, runner)

    assert quarantine_identity(data_path.stat()) == original_identity
    assert (data_path / ".retained").read_text(encoding="utf-8") == "keep\n"
    assert prepositioned is not None
    assert prepositioned.read_text(encoding="utf-8") == "pre-positioned\n"
    assert len(runner.calls) == 1
    assert runner.results[0].returncode != 0


@pytest.mark.parametrize(
    "metadata_race",
    [
        "data-inode",
        "data-mode",
        "data-symlink",
        "archive-inode",
        "archive-mode",
        "archive-symlink",
    ],
)
def test_candidate_data_quarantine_helper_rejects_changed_bound_metadata(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_race: str,
) -> None:
    module = load_deploy_tool()
    app_root, _state_directory, data_path, _state_path = (
        prepare_candidate_quarantine_inputs(
            module,
            rename_noreplace_path,
            monkeypatch,
        )
    )
    displaced = app_root / f"displaced-{metadata_race}"

    def mutate_after_parent_validation(command: list[str]) -> None:
        assert len(command) == 20
        archive = app_root / command[7]
        if metadata_race == "data-inode":
            data_path.rename(displaced)
            data_path.mkdir(mode=0o700)
            (data_path / ".replacement").write_text("replacement\n", encoding="utf-8")
        elif metadata_race == "data-mode":
            data_path.chmod(0o755)
        elif metadata_race == "data-symlink":
            data_path.rename(displaced)
            data_path.symlink_to(displaced, target_is_directory=True)
        elif metadata_race == "archive-inode":
            archive.rename(displaced)
            archive.mkdir(mode=0o700)
        elif metadata_race == "archive-symlink":
            archive.rename(displaced)
            archive.symlink_to(displaced, target_is_directory=True)
        else:
            archive.chmod(0o755)

    runner = InlineSudoPythonRunner(
        module,
        before_execute=mutate_after_parent_validation,
    )

    with pytest.raises(module.DeploymentError):
        module.quarantine_candidate_inputs(VALID_SHA, runner)

    assert len(runner.calls) == 1
    assert runner.results[0].returncode != 0
    assert data_path.is_dir()
    if metadata_race == "data-inode":
        assert (displaced / ".retained").read_text(encoding="utf-8") == "keep\n"
        assert (data_path / ".replacement").read_text(encoding="utf-8") == (
            "replacement\n"
        )
    elif metadata_race == "data-symlink":
        assert data_path.is_symlink()
        assert (displaced / ".retained").read_text(encoding="utf-8") == "keep\n"
    else:
        assert (data_path / ".retained").read_text(encoding="utf-8") == "keep\n"


def test_candidate_data_quarantine_helper_is_dirfd_bound_and_durable() -> None:
    module = load_deploy_tool()
    helper = module.CANDIDATE_DATA_QUARANTINE_HELPER

    for required_contract in (
        "O_NOFOLLOW",
        "renameat2",
        "RENAME_NOREPLACE",
        "data_descriptor = os.open",
        "os.fstat(data_descriptor)",
        "os.fsync(archive_descriptor)",
        "os.fsync(root_descriptor)",
    ):
        assert required_contract in helper
    compile(helper, "<candidate-data-quarantine-helper>", "exec")

    rename_offset = helper.rindex("renameat2(")
    receipt_offset = helper.rindex("print(")
    assert helper.index("data_descriptor = os.open") < rename_offset
    assert helper.rindex("os.fstat(data_descriptor)") > rename_offset
    assert helper.rindex("os.close(data_descriptor)") > rename_offset
    assert rename_offset < helper.rindex("os.fsync(archive_descriptor)")
    assert rename_offset < helper.rindex("os.fsync(root_descriptor)")
    assert helper.rindex("os.fsync(archive_descriptor)") < receipt_offset
    assert helper.rindex("os.fsync(root_descriptor)") < receipt_offset


@pytest.mark.parametrize(
    "receipt_mutation",
    ["missing-newline", "extra-line", "noncanonical", "wrong-inode"],
)
def test_candidate_data_quarantine_requires_one_exact_identity_receipt(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_mutation: str,
) -> None:
    module = load_deploy_tool()
    app_root, _state_directory, data_path, state_path = (
        prepare_candidate_quarantine_inputs(
            module,
            rename_noreplace_path,
            monkeypatch,
            include_state=True,
        )
    )

    def corrupt_receipt(receipt: str) -> str:
        fields = receipt.removesuffix("\n").split("|")
        assert len(fields) == 4
        if receipt_mutation == "missing-newline":
            return receipt.removesuffix("\n")
        if receipt_mutation == "extra-line":
            return receipt + "unexpected\n"
        if receipt_mutation == "noncanonical":
            fields[0] = "0" + fields[0]
        else:
            fields[1] = str(int(fields[1]) + 1)
        return "|".join(fields) + "\n"

    runner = InlineSudoPythonRunner(module, rewrite_stdout=corrupt_receipt)

    with pytest.raises(module.DeploymentError, match="receipt|identity"):
        module.quarantine_candidate_inputs(VALID_SHA, runner)

    assert not data_path.exists()
    assert state_path.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert len(runner.calls) == 1
    archive = app_root / runner.calls[0][7]
    assert (archive / "data" / ".retained").read_text(encoding="utf-8") == ("keep\n")


@pytest.mark.parametrize(
    "unsafe_lock_shape",
    ["missing", "symlink", "directory", "wrong-mode", "hard-link"],
)
def test_quarantine_candidate_inputs_rejects_an_unsafe_prepare_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_lock_shape: str,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    lock_path = state_directory / f"candidate-{VALID_SHA[:12]}.prepare.lock"
    if unsafe_lock_shape == "symlink":
        target = tmp_path / "external-lock"
        target.write_text("", encoding="utf-8")
        target.chmod(0o600)
        lock_path.symlink_to(target)
    elif unsafe_lock_shape == "directory":
        lock_path.mkdir(mode=0o600)
    elif unsafe_lock_shape == "wrong-mode":
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o644)
    elif unsafe_lock_shape == "hard-link":
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o600)
        os.link(lock_path, tmp_path / "second-lock-link")
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)

    with pytest.raises(module.DeploymentError, match="prepare lock"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert data_path.is_dir()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_candidate_inputs_rejects_a_symlinked_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    app_root.mkdir(mode=0o700, parents=True)
    external_state = tmp_path / "external-state"
    external_state.mkdir(mode=0o700)
    state_directory = app_root / "deploy-state"
    state_directory.symlink_to(external_state, target_is_directory=True)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    lock_path = external_state / f"candidate-{VALID_SHA[:12]}.prepare.lock"
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o600)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)

    with pytest.raises(module.DeploymentError, match="symbolic link"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert data_path.is_dir()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


@pytest.mark.parametrize("unsafe_mode", [0o770, 0o707, 0o777])
def test_quarantine_candidate_inputs_rejects_a_group_or_world_writable_app_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_mode: int,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    app_root.chmod(unsafe_mode)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    create_candidate_prepare_lock(app_root)

    with pytest.raises(
        module.DeploymentError,
        match="application root.*(group|world|mode|writable)",
    ):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert app_root.stat().st_mode & 0o777 == unsafe_mode
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_rejects_cross_device_state_before_creating_an_archive(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = rename_noreplace_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    app_root.mkdir(mode=0o700, parents=True)
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    lock_path = create_candidate_prepare_lock(app_root)
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    state_path.write_text('{"keep": true}\n', encoding="utf-8")
    state_path.chmod(0o600)

    real_fstat = module.os.fstat
    real_lstat = module.os.lstat
    real_stat = module.os.stat
    state_fd = os.open(state_directory, os.O_RDONLY | os.O_DIRECTORY)
    lock_fd = os.open(lock_path, os.O_RDWR)
    state_metadata = real_fstat(state_fd)
    lock_metadata = real_fstat(lock_fd)
    fake_state_device = app_root.stat().st_dev + 1
    state_fields = list(state_metadata)
    state_fields[stat.ST_DEV] = fake_state_device
    cross_device_state = os.stat_result(state_fields)
    held_lock = module.CandidatePrepareLock(
        state_directory_fd=state_fd,
        state_directory_path=state_directory,
        state_directory_identity=(fake_state_device, state_metadata.st_ino),
        lock_fd=lock_fd,
        lock_name=lock_path.name,
        lock_identity=(lock_metadata.st_dev, lock_metadata.st_ino),
    )

    class SyntheticCrossDeviceLock:
        def __enter__(self) -> object:
            return held_lock

        def __exit__(self, *_error: object) -> bool:
            return False

    def cross_device_fstat(descriptor: int) -> os.stat_result:
        if descriptor == state_fd:
            return cross_device_state
        return real_fstat(descriptor)

    def cross_device_lstat(
        path: str | bytes | int | Path,
        *,
        dir_fd: int | None = None,
    ) -> os.stat_result:
        if path == state_directory or path == str(state_directory):
            return cross_device_state
        return real_lstat(path, dir_fd=dir_fd)

    def cross_device_stat(
        path: str | bytes | int | Path,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if (
            path == state_directory
            or path == str(state_directory)
            or (path == "deploy-state" and dir_fd is not None)
        ):
            return cross_device_state
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(
        module,
        "_candidate_prepare_lock",
        lambda *_args, **_kwargs: SyntheticCrossDeviceLock(),
    )
    monkeypatch.setattr(module.os, "fstat", cross_device_fstat)
    monkeypatch.setattr(module.os, "lstat", cross_device_lstat)
    monkeypatch.setattr(module.os, "stat", cross_device_stat)

    try:
        with pytest.raises(module.DeploymentError, match="same filesystem"):
            module.quarantine_candidate_inputs(VALID_SHA)
    finally:
        os.close(lock_fd)
        os.close(state_fd)

    assert state_path.is_file()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_rejects_a_cross_device_state_file_before_creating_an_archive(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = rename_noreplace_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    create_candidate_prepare_lock(app_root)
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    state_path.write_text('{"keep": true}\n', encoding="utf-8")
    state_path.chmod(0o600)
    real_metadata = module._candidate_input_metadata

    def cross_device_state_metadata(
        name: str,
        *,
        directory_fd: int,
        label: str,
    ) -> os.stat_result | None:
        metadata = real_metadata(
            name,
            directory_fd=directory_fd,
            label=label,
        )
        if label != "candidate state" or metadata is None:
            return metadata
        fields = list(metadata)
        fields[stat.ST_DEV] = app_root.stat().st_dev + 1
        return os.stat_result(fields)

    monkeypatch.setattr(
        module,
        "_candidate_input_metadata",
        cross_device_state_metadata,
    )

    with pytest.raises(module.DeploymentError, match="same filesystem"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert state_path.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_candidate_inputs_rejects_a_busy_prepare_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    lock_path = create_candidate_prepare_lock(app_root)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)

    with lock_path.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.DeploymentError, match="already in progress"):
            module.quarantine_candidate_inputs(VALID_SHA)

    assert data_path.is_dir()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_candidate_inputs_rejects_a_lock_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    lock_path = create_candidate_prepare_lock(app_root)
    original_lock_inode = lock_path.stat().st_ino
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)
    real_flock = module.fcntl.flock
    replaced = False

    def replace_lock_after_acquisition(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if operation & fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.write_text("replacement\n", encoding="utf-8")
            lock_path.chmod(0o600)

    monkeypatch.setattr(module.fcntl, "flock", replace_lock_after_acquisition)

    with pytest.raises(module.DeploymentError, match="changed while acquiring"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert lock_path.stat().st_ino != original_lock_inode
    assert data_path.is_dir()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_candidate_inputs_rejects_a_wrong_owner_prepare_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    lock_path = create_candidate_prepare_lock(app_root)
    lock_name = lock_path.name
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)
    real_stat = module.os.stat

    def stat_with_wrong_lock_owner(
        path: str | bytes | int | Path,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        metadata = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == lock_name and dir_fd is not None:
            fields = list(metadata)
            fields[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(module.os, "stat", stat_with_wrong_lock_owner)

    with pytest.raises(module.DeploymentError, match="prepare lock is unsafe"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert data_path.is_dir()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


@pytest.mark.parametrize(
    ("candidate_item", "broken"),
    [("data", False), ("data", True), ("state", False), ("state", True)],
)
def test_quarantine_candidate_inputs_never_follows_candidate_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_item: str,
    broken: bool,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    create_candidate_prepare_lock(app_root)
    link_path = (
        app_root / f"data-candidate-{VALID_SHA[:12]}"
        if candidate_item == "data"
        else state_directory / f"candidate-{VALID_SHA[:12]}.json"
    )
    target = tmp_path / f"{candidate_item}-target"
    if not broken:
        if candidate_item == "data":
            target.mkdir(mode=0o700)
        else:
            target.write_text("retain\n", encoding="utf-8")
            target.chmod(0o600)
    link_path.symlink_to(target, target_is_directory=candidate_item == "data")

    with pytest.raises(module.DeploymentError, match=f"candidate {candidate_item}"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert link_path.is_symlink()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


@pytest.mark.parametrize(
    "unsafe_input",
    [
        "data-file",
        "data-mode",
        "data-owner",
        "state-directory",
        "state-mode",
        "state-hard-link",
    ],
)
def test_quarantine_candidate_inputs_rejects_unsafe_input_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_input: str,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    create_candidate_prepare_lock(app_root)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"

    if unsafe_input == "data-file":
        data_path.write_text("not a directory\n", encoding="utf-8")
        data_path.chmod(0o700)
    elif unsafe_input == "data-mode":
        data_path.mkdir(mode=0o700)
        data_path.chmod(0o755)
    elif unsafe_input == "data-owner":
        data_path.mkdir(mode=0o700)
        monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid() + 1)
    elif unsafe_input == "state-directory":
        state_path.mkdir(mode=0o700)
        state_path.chmod(0o600)
    else:
        state_path.write_text('{"keep": true}\n', encoding="utf-8")
        state_path.chmod(0o644 if unsafe_input == "state-mode" else 0o600)
        if unsafe_input == "state-hard-link":
            os.link(state_path, tmp_path / "second-state-link")

    with pytest.raises(module.DeploymentError, match="candidate (data|state)"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert data_path.exists() or state_path.exists()
    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_candidate_inputs_refuses_no_inputs_without_empty_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    create_candidate_prepare_lock(app_root)

    with pytest.raises(module.DeploymentError, match="no candidate data or state"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert not list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))


def test_quarantine_candidate_inputs_resumes_without_reusing_the_first_archive(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    tmp_path = rename_noreplace_path
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(module, "RUNTIME_GID", os.getegid())
    create_candidate_prepare_lock(app_root)
    data_path = app_root / f"data-candidate-{VALID_SHA[:12]}"
    data_path.mkdir(mode=0o700)
    retained = data_path / ".retained"
    retained.write_text("keep\n", encoding="utf-8")
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    state_path.write_text('{"keep": true}\n', encoding="utf-8")
    state_path.chmod(0o600)
    real_rename = module._rename_candidate_input_noreplace

    def interrupt_before_state_move(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        if source == state_path.name:
            raise OSError("simulated interruption")
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        module,
        "_rename_candidate_input_noreplace",
        interrupt_before_state_move,
    )
    first_runner = InlineSudoPythonRunner(module)
    with pytest.raises(module.DeploymentError, match="candidate state"):
        module.quarantine_candidate_inputs(VALID_SHA, first_runner)

    first_archives = list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))
    assert len(first_archives) == 1
    first_archive = first_archives[0]
    assert (first_archive / "data" / ".retained").read_text(encoding="utf-8") == (
        "keep\n"
    )
    assert state_path.is_file()

    monkeypatch.setattr(module, "_rename_candidate_input_noreplace", real_rename)
    second_runner = InlineSudoPythonRunner(module)
    second_archive = module.quarantine_candidate_inputs(VALID_SHA, second_runner)

    assert second_archive != first_archive
    assert (first_archive / "data" / ".retained").read_text(encoding="utf-8") == (
        "keep\n"
    )
    assert (second_archive / state_path.name).read_text(encoding="utf-8") == (
        '{"keep": true}\n'
    )
    assert not state_path.exists()
    assert len(first_runner.calls) == 1
    assert second_runner.calls == []


def test_candidate_quarantine_rename_never_replaces_an_existing_destination(
    rename_noreplace_path: Path,
) -> None:
    module = load_deploy_tool()
    tmp_path = rename_noreplace_path
    source_directory = tmp_path / "source"
    destination_directory = tmp_path / "destination"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / "candidate.json"
    destination = destination_directory / "candidate.json"
    source.write_text("candidate\n", encoding="utf-8")
    destination.write_text("pre-positioned\n", encoding="utf-8")
    source_fd = os.open(source_directory, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(destination_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            module._rename_candidate_input_noreplace(
                source.name,
                destination.name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert source.read_text(encoding="utf-8") == "candidate\n"
    assert destination.read_text(encoding="utf-8") == "pre-positioned\n"


def test_quarantine_candidate_inputs_refuses_a_racing_archive_destination(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    tmp_path = rename_noreplace_path
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700, parents=True)
    app_root.chmod(0o700)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    create_candidate_prepare_lock(app_root)
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    state_path.write_text('{"keep": true}\n', encoding="utf-8")
    state_path.chmod(0o600)
    real_rename = module._rename_candidate_input_noreplace

    def inject_destination_then_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, b"pre-positioned\n")
        finally:
            os.close(descriptor)
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        module,
        "_rename_candidate_input_noreplace",
        inject_destination_then_rename,
    )

    with pytest.raises(module.DeploymentError, match="candidate state"):
        module.quarantine_candidate_inputs(VALID_SHA)

    assert state_path.read_text(encoding="utf-8") == '{"keep": true}\n'
    archives = list(app_root.glob(f"quarantine-candidate-{VALID_SHA[:12]}-*"))
    assert len(archives) == 1
    assert (archives[0] / state_path.name).read_text(encoding="utf-8") == (
        "pre-positioned\n"
    )


@pytest.mark.parametrize(
    "candidate_shape",
    [
        "regular-file",
        "empty-directory",
        "hidden-file",
        "subdirectory",
        "symbolic-link",
        "broken-symbolic-link",
    ],
)
def test_prepare_rejects_every_preexisting_candidate_path_before_any_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_shape: str,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    product_root = app_root / "code"
    state_directory = app_root / "deploy-state"
    live = app_root / "data-live"
    candidate = app_root / "data-candidate-0123456789ab"
    write_deployment_assets(product_root)
    for directory in (state_directory, live):
        directory.mkdir(mode=0o700, parents=True)
    if candidate_shape == "regular-file":
        candidate.write_text("stale data\n", encoding="utf-8")
    elif candidate_shape == "symbolic-link":
        target = app_root / "candidate-target"
        target.mkdir(mode=0o700)
        candidate.symlink_to(target, target_is_directory=True)
    elif candidate_shape == "broken-symbolic-link":
        candidate.symlink_to(app_root / "missing-candidate", target_is_directory=True)
    else:
        candidate.mkdir(mode=0o700)
    if candidate_shape == "hidden-file":
        (candidate / ".stale").write_text("stale data\n", encoding="utf-8")
    elif candidate_shape == "subdirectory":
        (candidate / "nested").mkdir()
    manifest = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "approved_remote_ref": "refs/remotes/origin/main",
        "image_reference": f"commerce-ops-desk:{VALID_SHA}",
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "deployment_assets": deployment_asset_payload(product_root),
    }
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "_load_build_manifest", lambda _path: manifest)

    class NoCommandRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            pytest.fail(f"no command may run with stale candidate data: {arguments}")

    arguments = argparse.Namespace(
        build_manifest=str(state_directory / "manifest.json"),
        candidate_port="18088",
        live_data_dir=str(live),
        candidate_data_dir=str(candidate),
    )

    with pytest.raises(module.DeploymentError, match="must not already exist"):
        module.prepare_candidate(arguments, NoCommandRunner())


@pytest.mark.parametrize("state_shape", ["regular-file", "directory", "symbolic-link"])
def test_prepare_rejects_stale_candidate_state_before_any_docker_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_shape: str,
) -> None:
    module = load_deploy_tool()
    app_root = tmp_path / "apps" / "commerce-ops-desk"
    product_root = app_root / "code"
    state_directory = app_root / "deploy-state"
    live = app_root / "data-live"
    candidate = app_root / "data-candidate-0123456789ab"
    write_deployment_assets(product_root)
    for directory in (state_directory, live):
        directory.mkdir(mode=0o700, parents=True)
    stale_state = state_directory / "candidate-0123456789ab.json"
    if state_shape == "regular-file":
        stale_state.write_text('{"stale": true}\n', encoding="utf-8")
        stale_state.chmod(0o600)
    elif state_shape == "directory":
        stale_state.mkdir()
    else:
        stale_state.symlink_to(tmp_path / "missing-state")
    manifest = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "approved_remote_ref": "refs/remotes/origin/main",
        "image_reference": f"commerce-ops-desk:{VALID_SHA}",
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "deployment_assets": deployment_asset_payload(product_root),
    }
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "_load_build_manifest", lambda _path: manifest)

    class NoDockerRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            pytest.fail(f"Docker must not run with stale candidate state: {arguments}")

    arguments = argparse.Namespace(
        build_manifest=str(state_directory / "manifest.json"),
        candidate_port="18088",
        live_data_dir=str(live),
        candidate_data_dir=str(candidate),
    )

    with pytest.raises(module.DeploymentError, match="candidate state already exists"):
        module.prepare_candidate(arguments, NoDockerRunner())


def test_prepare_uses_one_verified_compose_service_and_records_final_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    (tmp_path / "live").mkdir()
    contents = write_deployment_assets(product_root)
    manifest = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "approved_remote_ref": "refs/remotes/origin/main",
        "image_reference": f"commerce-ops-desk:{VALID_SHA}",
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "deployment_assets": deployment_asset_payload(product_root),
    }
    identity = module.candidate_identity(VALID_SHA)
    network_name = f"{identity.project_name}_default"
    create_container_id = "1" * 64
    final_container_id = "2" * 64
    network_inspection = [
        {
            "Name": network_name,
            "Labels": {
                "com.docker.compose.project": identity.project_name,
                "com.docker.compose.network": "default",
            },
            "IPAM": {"Config": [{"Gateway": "192.0.2.1", "Subnet": "192.0.2.0/24"}]},
            "Containers": {
                final_container_id: {"Name": identity.container_name},
            },
        }
    ]
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    monkeypatch.setattr(module, "_load_build_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        module,
        "_candidate_data_path_for_creation",
        lambda **_: tmp_path / "candidate",
    )
    monkeypatch.setattr(
        module,
        "validate_data_directories",
        lambda **_: (tmp_path / "live", tmp_path / "candidate"),
    )
    monkeypatch.setattr(module, "validate_deployment_layout", lambda **_: None)
    live_data_identity = (64_769, 55_451_026, module.RUNTIME_UID, module.RUNTIME_GID)
    candidate_data_identity = (
        64_769,
        55_451_027,
        module.RUNTIME_UID,
        module.RUNTIME_GID,
    )
    candidate_creations: list[object] = []

    def create_candidate(
        _runner: object,
        *,
        app_root: Path,
        identity: object,
    ) -> tuple[Path, tuple[int, int, int, int]]:
        candidate_creations.append((app_root, identity))
        return tmp_path / "candidate", candidate_data_identity

    monkeypatch.setattr(module, "_create_candidate_data_directory", create_candidate)
    monkeypatch.setattr(
        module,
        "_data_directory_identity",
        lambda path: (
            live_data_identity if path.name == "live" else candidate_data_identity
        ),
    )
    monkeypatch.setattr(
        module, "_assert_data_directory_identity", lambda *_args, **_kwargs: None
    )
    daemon_checks: list[str] = []
    monkeypatch.setattr(
        module,
        "_assert_local_docker_daemon",
        lambda _runner, expected: daemon_checks.append(expected),
        raising=False,
    )
    monkeypatch.setattr(
        module, "_assert_image_revision", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(module, "_immutable_image_config", lambda *_args: {})
    monkeypatch.setattr(module, "_assert_candidate_absent", lambda *_args: None)
    monkeypatch.setattr(module, "_assert_port_available", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_created_project_network",
        lambda *_args: (network_name, network_inspection),
    )
    exact_container_calls: list[str] = []
    identifiers = iter((create_container_id, final_container_id))

    def exact_container(_runner: object, _identity: object) -> str:
        identifier = next(identifiers)
        exact_container_calls.append(identifier)
        return identifier

    monkeypatch.setattr(module, "_assert_exact_project_container", exact_container)
    final_network_ids: list[str | None] = []

    def validate_network(
        _identity: object,
        _network_name: str,
        inspection: object,
        *,
        expected_container_id: str | None,
    ) -> object:
        assert inspection == network_inspection
        final_network_ids.append(expected_container_id)
        return module.CandidateNetworkContract(
            network_id="a" * 64,
            subnet="192.0.2.0/24",
            gateway="192.0.2.1",
            endpoint_id=None if expected_container_id is None else "c" * 64,
            ipv4_address=None if expected_container_id is None else "192.0.2.2/24",
        )

    monkeypatch.setattr(
        module, "_validate_candidate_network_contract", validate_network
    )
    monkeypatch.setattr(module, "_wait_for_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module, "_assert_candidate_runtime", lambda *_args, **_kwargs: "4" * 64
    )
    written: dict[str, object] = {}

    def publish_state(_path: Path, payload: Mapping[str, object]) -> None:
        with (
            pytest.raises(module.DeploymentError, match="already in progress"),
            module._candidate_prepare_lock(VALID_SHA),
        ):
            pytest.fail("prepare released its SHA lock before state publication")
        written.update(payload)

    monkeypatch.setattr(
        module,
        "_write_private_json",
        publish_state,
    )

    class ComposeRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], str | None]] = []

        def run(
            self,
            arguments: list[str],
            *,
            input_text: str | None = None,
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.calls.append((command, input_text))
            if command[-2:] == ["config", "--services"]:
                return subprocess.CompletedProcess(
                    command, 0, "commerce-ops-desk\n", ""
                )
            if command[1:3] == ["network", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(network_inspection), ""
                )
            return subprocess.CompletedProcess(command, 0, "", "")

    runner = ComposeRunner()
    arguments = argparse.Namespace(
        build_manifest=str(tmp_path / "manifest.json"),
        candidate_port="18088",
        live_data_dir=str(tmp_path / "live"),
        candidate_data_dir=str(tmp_path / "candidate"),
    )
    module.prepare_candidate(arguments, runner)

    compose_calls = [call for call in runner.calls if call[0][1] == "compose"]
    assert [call[0][6:] for call in compose_calls] == [
        ["config", "--services"],
        ["create", "--no-build", "commerce-ops-desk"],
        [
            "up",
            "--detach",
            "--force-recreate",
            "--no-build",
            "--no-deps",
            "commerce-ops-desk",
        ],
    ]
    assert all(call[1] == contents[DEPLOYMENT_ASSET_PATHS[0]] for call in compose_calls)
    assert exact_container_calls == [create_container_id, final_container_id]
    assert final_network_ids[-1] == final_container_id
    assert daemon_checks == [DOCKER_DAEMON_ID, DOCKER_DAEMON_ID]
    assert written["schema"] == 2
    assert written["container_id"] == final_container_id
    assert written["docker_daemon_id"] == DOCKER_DAEMON_ID
    assert written["candidate_data_device"] == candidate_data_identity[0]
    assert written["candidate_data_inode"] == candidate_data_identity[1]
    assert written["live_data_device"] == live_data_identity[0]
    assert written["live_data_inode"] == live_data_identity[1]
    assert written["network_id"] == "a" * 64
    assert written["network_endpoint_id"] == "c" * 64
    assert written["runtime_sha256"] == "4" * 64
    assert written["deployment_assets"] == manifest["deployment_assets"]
    assert candidate_creations == [(module.APP_ROOT, identity)]
    with module._candidate_prepare_lock(VALID_SHA):
        pass


def test_state_revalidation_rejects_a_recreated_candidate_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    write_deployment_assets(product_root)
    identity = module.candidate_identity(VALID_SHA)
    prepared_container_id = "1" * 64
    replacement_container_id = "2" * 64
    state = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "project_name": identity.project_name,
        "container_name": identity.container_name,
        "container_id": prepared_container_id,
        "image": identity.image,
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "candidate_port": 18088,
        "candidate_data_dir": str(tmp_path / "candidate"),
        "candidate_data_device": 64_769,
        "candidate_data_inode": 55_451_027,
        "live_data_dir": str(tmp_path / "live"),
        "live_data_device": 64_769,
        "live_data_inode": 55_451_026,
        "network_name": f"{identity.project_name}_default",
        "network_id": "2" * 64,
        "network_endpoint_id": "3" * 64,
        "runtime_sha256": "4" * 64,
        "trusted_proxy_cidrs": ["192.0.2.1/32"],
        "deployment_assets": deployment_asset_payload(product_root),
    }
    monkeypatch.setattr(module, "PRODUCT_ROOT", product_root)
    monkeypatch.setattr(
        module,
        "validate_data_directories",
        lambda **_: (tmp_path / "live", tmp_path / "candidate"),
    )
    monkeypatch.setattr(module, "validate_deployment_layout", lambda **_: None)
    monkeypatch.setattr(
        module, "_assert_data_directory_identity", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(module, "_assert_local_docker_daemon", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_assert_exact_project_container",
        lambda *_args: replacement_container_id,
    )
    monkeypatch.setattr(
        module,
        "_assert_candidate_network",
        lambda *_args, **_kwargs: '["192.0.2.1/32"]',
    )
    monkeypatch.setattr(
        module, "_assert_image_revision", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(module, "_immutable_image_config", lambda *_args: {})
    monkeypatch.setattr(module, "_wait_for_healthy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module, "_assert_candidate_runtime", lambda *_args, **_kwargs: None
    )

    class NetworkRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, "[]", "")

    with pytest.raises(module.DeploymentError, match="container changed"):
        module._assert_state_candidate_ready(NetworkRunner(), state)


@pytest.mark.parametrize(
    ("current_network_id", "current_endpoint_id"),
    [
        ("5" * 64, "3" * 64),
        ("2" * 64, "6" * 64),
    ],
    ids=("network-id", "endpoint-id"),
)
def test_state_revalidation_rejects_network_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_network_id: str,
    current_endpoint_id: str,
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    write_deployment_assets(product_root)
    state = candidate_state_payload(
        module,
        product_root=product_root,
        temporary_root=tmp_path,
    )
    runner = stub_state_candidate_ready_dependencies(
        module,
        monkeypatch,
        product_root=product_root,
        temporary_root=tmp_path,
        current_network_id=current_network_id,
        current_endpoint_id=current_endpoint_id,
    )

    with pytest.raises(module.DeploymentError, match="network identity changed"):
        module._assert_state_candidate_ready(runner, state)


@pytest.mark.parametrize(
    ("directory", "identity_index", "label"),
    [
        ("candidate", 0, "candidate data directory"),
        ("candidate", 1, "candidate data directory"),
        ("live", 0, "live data directory"),
        ("live", 1, "live data directory"),
    ],
    ids=(
        "candidate-device",
        "candidate-inode",
        "live-device",
        "live-inode",
    ),
)
def test_state_revalidation_rejects_data_directory_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    identity_index: int,
    label: str,
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    write_deployment_assets(product_root)
    state = candidate_state_payload(
        module,
        product_root=product_root,
        temporary_root=tmp_path,
    )
    identities = {
        "candidate": [
            64_769,
            55_451_027,
            module.RUNTIME_UID,
            module.RUNTIME_GID,
        ],
        "live": [
            64_769,
            55_451_026,
            module.RUNTIME_UID,
            module.RUNTIME_GID,
        ],
    }
    identities[directory][identity_index] += 1
    runner = stub_state_candidate_ready_dependencies(
        module,
        monkeypatch,
        product_root=product_root,
        temporary_root=tmp_path,
        current_candidate_identity=tuple(identities["candidate"]),
        current_live_identity=tuple(identities["live"]),
    )

    with pytest.raises(module.DeploymentError, match=rf"{label} identity changed"):
        module._assert_state_candidate_ready(runner, state)


def test_state_revalidation_rejects_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    write_deployment_assets(product_root)
    state = candidate_state_payload(
        module,
        product_root=product_root,
        temporary_root=tmp_path,
    )
    runner = stub_state_candidate_ready_dependencies(
        module,
        monkeypatch,
        product_root=product_root,
        temporary_root=tmp_path,
        current_runtime_sha256="7" * 64,
    )

    with pytest.raises(module.DeploymentError, match="runtime identity changed"):
        module._assert_state_candidate_ready(runner, state)


def test_state_revalidation_success_propagates_the_exact_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
    write_deployment_assets(product_root)
    state = candidate_state_payload(
        module,
        product_root=product_root,
        temporary_root=tmp_path,
    )
    runner = stub_state_candidate_ready_dependencies(
        module,
        monkeypatch,
        product_root=product_root,
        temporary_root=tmp_path,
    )
    observed: dict[str, object] = {}

    def assert_runtime(
        actual_runner: object,
        actual_identity: object,
        *,
        expected_container_id: str,
        expected_image_id: str,
        network_name: str,
        port: int,
        data_directory: Path,
        data_identity: tuple[int, int, int, int],
        trusted_proxy_json: str,
        image_config: Mapping[str, object],
    ) -> str:
        observed.update(
            runner=actual_runner,
            identity=actual_identity,
            expected_container_id=expected_container_id,
            expected_image_id=expected_image_id,
            network_name=network_name,
            port=port,
            data_directory=data_directory,
            data_identity=data_identity,
            trusted_proxy_json=trusted_proxy_json,
            image_config=image_config,
        )
        return "4" * 64

    monkeypatch.setattr(module, "_assert_candidate_runtime", assert_runtime)

    verified = module._assert_state_candidate_ready(runner, state)

    assert verified.deployment_assets.identity == state["deployment_assets"]
    assert observed == {
        "runner": runner,
        "identity": module.candidate_identity(VALID_SHA),
        "expected_container_id": "1" * 64,
        "expected_image_id": "sha256:" + "1" * 64,
        "network_name": f"{module.candidate_identity(VALID_SHA).project_name}_default",
        "port": 18088,
        "data_directory": tmp_path / "candidate",
        "data_identity": (
            64_769,
            55_451_027,
            module.RUNTIME_UID,
            module.RUNTIME_GID,
        ),
        "trusted_proxy_json": '["192.0.2.1/32"]',
        "image_config": {},
    }


def test_private_state_requires_owner_mode_single_link_and_canonical_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "candidate-0123456789ab.json"
    payload = {
        "schema": 2,
        "source_sha": VALID_SHA,
        "project_name": "commerce-ops-candidate-0123456789ab",
        "container_name": "app-commerce-ops-desk-candidate-0123456789ab",
        "container_id": "1" * 64,
        "image": f"commerce-ops-desk:{VALID_SHA}",
        "image_id": "sha256:" + "1" * 64,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "candidate_port": 18088,
        "candidate_data_dir": (
            "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab"
        ),
        "candidate_data_device": 64_769,
        "candidate_data_inode": 55_451_027,
        "live_data_dir": "/home/deploy/apps/commerce-ops-desk/data-live",
        "live_data_device": 64_769,
        "live_data_inode": 55_451_026,
        "network_name": "commerce-ops-candidate-0123456789ab_default",
        "network_id": "2" * 64,
        "network_endpoint_id": "3" * 64,
        "runtime_sha256": "4" * 64,
        "trusted_proxy_cidrs": ["192.0.2.1/32"],
        "deployment_assets": deployment_asset_payload(),
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    state_path.chmod(0o600)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)

    assert module._load_candidate_state(state_path)["source_sha"] == VALID_SHA

    state_path.chmod(0o640)
    with pytest.raises(module.DeploymentError, match="0600"):
        module._load_candidate_state(state_path)
    state_path.chmod(0o600)

    hardlink = state_directory / "copy.json"
    os.link(state_path, hardlink)
    with pytest.raises(module.DeploymentError, match="one hard link"):
        module._load_candidate_state(state_path)
    hardlink.unlink()

    owner = state_path.stat().st_uid
    monkeypatch.setattr(module.os, "geteuid", lambda: owner + 1)
    with pytest.raises(module.DeploymentError, match="current user"):
        module._load_candidate_state(state_path)


def test_private_state_rejects_a_symlinked_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    real_directory = tmp_path / "real-state"
    real_directory.mkdir(mode=0o700)
    state_path = real_directory / "candidate-0123456789ab.json"
    state_path.write_text("{}", encoding="utf-8")
    state_path.chmod(0o600)
    linked_directory = tmp_path / "deploy-state"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(module, "STATE_DIRECTORY", linked_directory)

    with pytest.raises(module.DeploymentError, match="symbolic link"):
        module._load_candidate_state(linked_directory / state_path.name)


def test_candidate_state_atomic_write_refuses_a_prepositioned_temporary_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    state_directory = tmp_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "candidate.json"
    victim = tmp_path / "victim.json"
    victim.write_text("do not replace\n", encoding="utf-8")
    token = "0123456789abcdef"
    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: token)
    temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.{token}.tmp")
    temporary.symlink_to(victim)

    with pytest.raises(module.DeploymentError, match="could not be written safely"):
        module._write_private_json(state_path, {"schema": 2})

    assert victim.read_text(encoding="utf-8") == "do not replace\n"
    assert temporary.is_symlink()
    assert not state_path.exists()


def test_candidate_state_publication_never_clobbers_a_racing_state_file(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state_directory = rename_noreplace_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "candidate-0123456789ab.json"
    racing_content = '{"owner": "other-prepare"}\n'
    real_rename_noreplace = module._rename_candidate_input_noreplace

    def insert_racing_state(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        racing_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        with os.fdopen(racing_descriptor, "w", encoding="utf-8") as racing_state:
            racing_state.write(racing_content)
            racing_state.flush()
            os.fsync(racing_state.fileno())
        real_rename_noreplace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        module,
        "_rename_candidate_input_noreplace",
        insert_racing_state,
    )

    with pytest.raises(module.DeploymentError, match="already exists|published safely"):
        module._write_private_json(state_path, {"schema": 2})

    assert state_path.read_text(encoding="utf-8") == racing_content


def test_candidate_state_link_before_unlink_interruption_remains_quarantinable(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root = rename_noreplace_path / "commerce-ops-desk"
    app_root.mkdir(mode=0o700)
    state_directory = app_root / "deploy-state"
    state_directory.mkdir(mode=0o700)
    create_candidate_prepare_lock(app_root)
    monkeypatch.setattr(module, "APP_ROOT", app_root)
    monkeypatch.setattr(module, "STATE_DIRECTORY", state_directory)
    state_path = state_directory / f"candidate-{VALID_SHA[:12]}.json"
    real_link = os.link
    interrupted_exit = 73
    unexpected_exit = 74
    child = os.fork()
    if child == 0:

        def interrupt_after_hard_link(
            source: str | bytes | Path,
            destination: str | bytes | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            real_link(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )
            os._exit(interrupted_exit)

        module.os.link = interrupt_after_hard_link
        try:
            module._write_private_json(state_path, {"schema": 2})
        except BaseException:  # noqa: BLE001 - forked child must not re-enter pytest.
            os._exit(unexpected_exit)
        os._exit(0)

    waited_child, wait_status = os.waitpid(child, 0)
    assert waited_child == child
    assert os.WIFEXITED(wait_status)
    assert os.WEXITSTATUS(wait_status) in {0, interrupted_exit}

    archive = module.quarantine_candidate_inputs(VALID_SHA)

    archived_state = archive / state_path.name
    assert json.loads(archived_state.read_text(encoding="utf-8")) == {"schema": 2}
    assert not state_path.exists()
    assert not list(state_directory.glob(f".{state_path.name}.*.tmp"))


def test_candidate_state_publication_rejects_a_post_rename_inode_substitution(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    state_directory = rename_noreplace_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "candidate-0123456789ab.json"
    racing_content = '{"owner": "post-rename-racer"}\n'
    real_rename_noreplace = module._rename_candidate_input_noreplace

    def substitute_published_inode(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        real_rename_noreplace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        os.unlink(destination, dir_fd=dst_dir_fd)
        racing_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        with os.fdopen(racing_descriptor, "w", encoding="utf-8") as racing_state:
            racing_state.write(racing_content)

    monkeypatch.setattr(
        module,
        "_rename_candidate_input_noreplace",
        substitute_published_inode,
    )

    with pytest.raises(module.DeploymentError, match="published safely"):
        module._write_private_json(state_path, {"schema": 2})

    assert state_path.read_text(encoding="utf-8") == racing_content


@pytest.mark.parametrize(
    "metadata_race",
    ["mode-before-open", "mode-after-open", "owner-after-open"],
)
def test_candidate_state_publication_rejects_metadata_changes_after_rename(
    rename_noreplace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_race: str,
) -> None:
    module = load_deploy_tool()
    state_directory = rename_noreplace_path / "deploy-state"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "candidate-0123456789ab.json"
    real_open = module.os.open
    real_fstat = module.os.fstat
    published_descriptor = -1

    def race_while_opening_published_state(
        path: str | bytes | int | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal published_descriptor
        if path == state_path.name and metadata_race == "mode-before-open":
            assert dir_fd is not None
            os.chmod(
                path,
                0o644,
                dir_fd=dir_fd,
                follow_symlinks=False,
            )
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == state_path.name:
            published_descriptor = descriptor
        return descriptor

    def race_after_open(descriptor: int) -> os.stat_result:
        if descriptor == published_descriptor and metadata_race == "mode-after-open":
            os.fchmod(descriptor, 0o644)
        metadata = real_fstat(descriptor)
        if descriptor == published_descriptor and metadata_race == "owner-after-open":
            fields = list(metadata)
            fields[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(module.os, "open", race_while_opening_published_state)
    monkeypatch.setattr(module.os, "fstat", race_after_open)

    with pytest.raises(module.DeploymentError, match="published safely"):
        module._write_private_json(state_path, {"schema": 2})

    assert state_path.is_file()
    if metadata_race.startswith("mode-"):
        assert state_path.stat().st_mode & 0o777 == 0o644


def test_rollback_rejects_a_user_writable_filename_hash_as_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    transaction_directory = tmp_path / "root-transactions"
    monkeypatch.setattr(module, "CADDY_TRANSACTION_DIRECTORY", transaction_directory)
    fragment = "http://commerce-ops-desk.srrsh.aig.rest { respond 200 }\n"
    user_directory = tmp_path / "user-caddy-backups"
    user_directory.mkdir(mode=0o700)
    backup = user_directory / (
        "commerce-ops-desk.20261009T120000Z-0123456789abcdef0123456789abcdef.conf"
    )
    backup.write_text(fragment, encoding="utf-8")
    backup.chmod(0o600)

    with pytest.raises(module.DeploymentError, match="trusted transaction directory"):
        module._load_validated_backup(object(), backup)


def test_rollback_rejects_legacy_or_manual_backup_names_before_reading_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    transaction_directory = tmp_path / "root-transactions"
    monkeypatch.setattr(module, "CADDY_TRANSACTION_DIRECTORY", transaction_directory)

    for name in (
        "commerce-ops-desk.manual.conf",
        "commerce-ops-desk.20261009T120000Z.before-test." + "a" * 64 + ".conf",
    ):
        with pytest.raises(module.DeploymentError, match="generated backup name"):
            module._load_validated_backup(object(), transaction_directory / name)


def test_deploy_tool_renders_exactly_one_caddy_port_placeholder() -> None:
    module = load_deploy_tool()
    template = "http://demo.example {\n reverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n}\n"

    rendered = module.render_caddy_template(template, 18088)

    assert rendered == "http://demo.example {\n reverse_proxy 127.0.0.1:18088\n}\n"
    for invalid_template in (
        "http://demo.example { respond 200 }",
        template + "# {{UPSTREAM_PORT}}\n",
    ):
        with pytest.raises(module.DeploymentError, match="exactly one"):
            module.render_caddy_template(invalid_template, 18088)


def test_hardened_caddy_template_binds_one_canonical_route_revision() -> None:
    module = load_deploy_tool()
    template = CADDY_TEMPLATE.read_text(encoding="utf-8")
    revision = "a" * 64

    rendered = module.render_caddy_template(
        template,
        18088,
        route_revision=revision,
    )

    assert "{{UPSTREAM_PORT}}" not in rendered
    assert "{{ROUTE_REVISION}}" not in rendered
    assert rendered.count(revision) == 1
    with pytest.raises(module.DeploymentError, match="route revision"):
        module.render_caddy_template(template, 18088)
    with pytest.raises(module.DeploymentError, match="route revision"):
        module.render_caddy_template(template, 18088, route_revision="not-a-hash")


def test_deploy_tool_cli_has_no_manual_trusted_proxy_override() -> None:
    module = load_deploy_tool()
    parser = module.build_parser()

    help_text = parser.format_help()
    prepare = parser.parse_args(
        [
            "prepare",
            "--build-manifest",
            "/home/deploy/apps/commerce-ops-desk/deploy-state/build-verified.json",
            "--candidate-port",
            "18088",
            "--live-data-dir",
            "/home/deploy/apps/commerce-ops-desk/data-live",
            "--candidate-data-dir",
            "/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab",
        ]
    )

    assert prepare.command == "prepare"
    assert not hasattr(prepare, "source_sha")
    assert "trusted-proxy" not in help_text


def test_deploy_tool_cli_exposes_only_the_source_sha_for_quarantine() -> None:
    module = load_deploy_tool()
    parser = module.build_parser()

    quarantine = parser.parse_args(["quarantine", "--source-sha", VALID_SHA])

    assert quarantine.command == "quarantine"
    assert quarantine.source_sha == VALID_SHA
    assert vars(quarantine) == {
        "command": "quarantine",
        "source_sha": VALID_SHA,
    }


def test_deploy_tool_cli_dispatches_quarantine_and_prints_the_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_deploy_tool()
    archive = tmp_path / f"quarantine-candidate-{VALID_SHA[:12]}-retained"
    observed_shas: list[str] = []
    runner = object()

    def quarantine(source_sha: str, received_runner: object) -> Path:
        observed_shas.append(source_sha)
        assert received_runner is runner
        return archive

    monkeypatch.setattr(module, "CommandRunner", lambda: runner)
    monkeypatch.setattr(module, "quarantine_candidate_inputs", quarantine)

    assert module.main(["quarantine", "--source-sha", VALID_SHA]) == 0
    assert observed_shas == [VALID_SHA]
    assert capsys.readouterr().out == f"candidate inputs retained at {archive}\n"


def test_deploy_tool_help_is_executable_without_docker_or_sudo() -> None:
    assert DEPLOY_TOOL.is_file()

    result = subprocess.run(
        [sys.executable, str(DEPLOY_TOOL), "--help"],
        cwd=PRODUCT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prepare" in result.stdout
    assert "quarantine" in result.stdout
    assert "switch" in result.stdout
    assert "rollback" in result.stdout


def test_runbook_is_explicit_about_blue_green_and_gateway_limitations() -> None:
    assert RUNBOOK.is_file(), "the workstation deployment runbook must be versioned"
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for required_phrase in (
        "prepare",
        "switch",
        "rollback",
        "data-candidate-",
        "10001:10001",
        "0700",
        "single /32",
        "shared source bucket",
        "must not claim independent visitor rate limiting",
        "/etc/caddy/sites/commerce-ops-desk.conf",
        'CODE_ROOT="$APP_ROOT/code"',
        'cd -- "$CODE_ROOT"',
        "schema-2 manifest",
        "Docker daemon ID",
        "--file -",
        "fixed-argv isolated privileged helper",
        "atomic no-clobber operation",
    ):
        assert required_phrase in runbook
    assert "sudo chown 10001:10001" not in runbook
    assert "sudo chmod 0700" not in runbook
    assert "access code" in runbook.lower()
    assert "do not put" in runbook.lower()

    cleanup = runbook.split("If preparation fails", 1)[1].split(
        "The candidate data directory", 1
    )[0]
    assert "--file deploy/workstation/compose.yaml" not in cleanup
    for cleanup_contract in (
        "/usr/bin/env -i",
        "DOCKER_HOST=unix:///var/run/docker.sock",
        "DOCKER_CONFIG=/etc/docker",
        "com.docker.compose.project",
        "com.docker.compose.service",
        "com.docker.compose.network",
        "CONTAINER_NAME",
        "NETWORK_NAME",
        'container rm --force "$CONTAINER_ID"',
        'network rm "$NETWORK_ID"',
    ):
        assert cleanup_contract in cleanup


@pytest.mark.parametrize(
    ("resource_shape", "expected_removals"),
    [
        ("network-only", ["network network-id"]),
        ("container-only", ["container container-id"]),
        ("both", ["container container-id", "network network-id"]),
    ],
)
def test_runbook_cleanup_handles_each_partial_compose_resource_shape(
    tmp_path: Path,
    resource_shape: str,
    expected_removals: list[str],
) -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    cleanup = runbook.split("If preparation fails", 1)[1].split(
        "The candidate data directory", 1
    )[0]
    recipe = re.search(r"```bash\n(.*?)\n```", cleanup, re.DOTALL)
    assert recipe is not None
    script = re.sub(
        r"DOCKER=\(\n.*?\n\)",
        'DOCKER=("$PYTHON_BIN" "$FAKE_DOCKER")',
        recipe.group(1),
        count=1,
        flags=re.DOTALL,
    )
    assert 'DOCKER=("$PYTHON_BIN" "$FAKE_DOCKER")' in script

    fake_docker = tmp_path / "fake_docker.py"
    removal_log = tmp_path / "removals.log"
    fake_docker.write_text(
        """\
import os
import sys

arguments = sys.argv[1:]
shape = os.environ["FAKE_RESOURCE_SHAPE"]
container_present = shape in {"container-only", "both"}
network_present = shape in {"network-only", "both"}

project_filter = "label=com.docker.compose.project=commerce-ops-candidate-0123456789ab"

if arguments == ["ps", "--all", "--quiet", "--filter", project_filter]:
    if container_present:
        print("container-id")
elif arguments == ["network", "ls", "--quiet", "--filter", project_filter]:
    if network_present:
        print("network-id")
elif arguments[0] == "inspect" and arguments[1] == "--format":
    rendered = {
        "{{.Name}}": "/app-commerce-ops-desk-candidate-0123456789ab",
        '{{index .Config.Labels "com.docker.compose.project"}}': (
            "commerce-ops-candidate-0123456789ab"
        ),
        '{{index .Config.Labels "com.docker.compose.service"}}': (
            "commerce-ops-desk"
        ),
    }
    print(rendered[arguments[2]])
elif arguments[0:2] == ["network", "inspect"]:
    rendered = {
        "{{.Name}}": "commerce-ops-candidate-0123456789ab_default",
        '{{index .Labels "com.docker.compose.project"}}': (
            "commerce-ops-candidate-0123456789ab"
        ),
        '{{index .Labels "com.docker.compose.network"}}': "default",
    }
    print(rendered[arguments[3]])
elif arguments[0:3] == ["container", "rm", "--force"]:
    with open(os.environ["FAKE_REMOVAL_LOG"], "a", encoding="utf-8") as output:
        output.write(f"container {arguments[3]}\\n")
elif arguments[0:2] == ["network", "rm"]:
    with open(os.environ["FAKE_REMOVAL_LOG"], "a", encoding="utf-8") as output:
        output.write(f"network {arguments[2]}\\n")
else:
    raise SystemExit(f"unexpected fake Docker arguments: {arguments!r}")
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash"],
        cwd=PRODUCT_ROOT,
        env={
            **os.environ,
            "SOURCE_SHA": VALID_SHA,
            "PYTHON_BIN": sys.executable,
            "FAKE_DOCKER": str(fake_docker),
            "FAKE_RESOURCE_SHAPE": resource_shape,
            "FAKE_REMOVAL_LOG": str(removal_log),
        },
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert removal_log.read_text(encoding="utf-8").splitlines() == expected_removals


@pytest.mark.parametrize(
    "failure_shape",
    [
        "none",
        "wrong-container-label",
        "wrong-network-name",
        "multiple-containers",
        "multiple-networks",
    ],
)
def test_runbook_cleanup_refuses_ambiguous_or_mislabeled_resources_before_removal(
    tmp_path: Path,
    failure_shape: str,
) -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    cleanup = runbook.split("If preparation fails", 1)[1].split(
        "The candidate data directory", 1
    )[0]
    recipe = re.search(r"```bash\n(.*?)\n```", cleanup, re.DOTALL)
    assert recipe is not None
    script = re.sub(
        r"DOCKER=\(\n.*?\n\)",
        'DOCKER=("$PYTHON_BIN" "$FAKE_DOCKER")',
        recipe.group(1),
        count=1,
        flags=re.DOTALL,
    )
    fake_docker = tmp_path / "fake_docker.py"
    query_log = tmp_path / "queries.jsonl"
    removal_log = tmp_path / "removals.log"
    fake_docker.write_text(
        """\
import json
import os
import sys

arguments = sys.argv[1:]
shape = os.environ["FAKE_FAILURE_SHAPE"]
project = "commerce-ops-candidate-0123456789ab"
project_filter = f"label=com.docker.compose.project={project}"
with open(os.environ["FAKE_QUERY_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\\n")

if arguments == ["ps", "--all", "--quiet", "--filter", project_filter]:
    if shape == "none":
        pass
    elif shape == "multiple-containers":
        print("container-id-1")
        print("container-id-2")
    else:
        print("container-id")
elif arguments == ["network", "ls", "--quiet", "--filter", project_filter]:
    if shape == "none":
        pass
    elif shape == "multiple-networks":
        print("network-id-1")
        print("network-id-2")
    else:
        print("network-id")
elif arguments[0] == "inspect" and arguments[1] == "--format":
    rendered = {
        "{{.Name}}": "/app-commerce-ops-desk-candidate-0123456789ab",
        '{{index .Config.Labels "com.docker.compose.project"}}': (
            "wrong-project" if shape == "wrong-container-label" else project
        ),
        '{{index .Config.Labels "com.docker.compose.service"}}': (
            "commerce-ops-desk"
        ),
    }
    print(rendered[arguments[2]])
elif arguments[0:2] == ["network", "inspect"]:
    rendered = {
        "{{.Name}}": (
            "wrong-network"
            if shape == "wrong-network-name"
            else f"{project}_default"
        ),
        '{{index .Labels "com.docker.compose.project"}}': project,
        '{{index .Labels "com.docker.compose.network"}}': "default",
    }
    print(rendered[arguments[3]])
elif arguments[0:3] == ["container", "rm", "--force"]:
    with open(os.environ["FAKE_REMOVAL_LOG"], "a", encoding="utf-8") as output:
        output.write(f"container {arguments[3]}\\n")
elif arguments[0:2] == ["network", "rm"]:
    with open(os.environ["FAKE_REMOVAL_LOG"], "a", encoding="utf-8") as output:
        output.write(f"network {arguments[2]}\\n")
else:
    raise SystemExit(f"unexpected fake Docker arguments: {arguments!r}")
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash"],
        cwd=PRODUCT_ROOT,
        env={
            **os.environ,
            "SOURCE_SHA": VALID_SHA,
            "PYTHON_BIN": sys.executable,
            "FAKE_DOCKER": str(fake_docker),
            "FAKE_FAILURE_SHAPE": failure_shape,
            "FAKE_QUERY_LOG": str(query_log),
            "FAKE_REMOVAL_LOG": str(removal_log),
        },
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not removal_log.exists()
    queries = [json.loads(line) for line in query_log.read_text().splitlines()]
    expected_filter = (
        "label=com.docker.compose.project=commerce-ops-candidate-0123456789ab"
    )
    assert queries[:2] == [
        ["ps", "--all", "--quiet", "--filter", expected_filter],
        ["network", "ls", "--quiet", "--filter", expected_filter],
    ]


def test_runbook_requires_exact_build_identity_before_and_after_switch() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    candidate_acceptance = " ".join(
        runbook.split("Before switching, inspect only this candidate's logs", 1)[1]
        .split("If preparation fails", 1)[0]
        .split()
    )

    for required_phrase in (
        "http://127.0.0.1:18088/api/build",
        '"service": "commerce-ops-desk"',
        '"version": "0.2.1"',
        '"source_sha": source_sha',
        "Cache-Control",
        "exactly `no-store`",
        "$SOURCE_SHA",
        "Do not weaken Secure cookies",
        "second publication path",
    ):
        assert required_phrase in candidate_acceptance

    external_acceptance = " ".join(
        runbook.split("## 6. External acceptance", 1)[1]
        .split("## 7. Roll back", 1)[0]
        .split()
    )
    for required_phrase in (
        "after the route switch",
        "/api/build",
        "0.2.1",
        "SOURCE_SHA",
        "Cache-Control",
        "no-store",
    ):
        assert required_phrase in external_acceptance


def test_runbook_requires_one_explicit_deploy_sha_on_origin_and_github_main() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    stage = runbook.split("## 1. Stage the exact source and image", 1)[1].split(
        "## 2. Prepare independent state", 1
    )[0]

    for required_contract in (
        "set -euo pipefail",
        "${DEPLOY_SHA:?",
        "refs/remotes/origin/main",
        "refs/remotes/github/main",
        'ORIGIN_MAIN_SHA="$(',
        'GITHUB_MAIN_SHA="$(',
        'test "$ORIGIN_MAIN_SHA" = "$DEPLOY_SHA"',
        'test "$GITHUB_MAIN_SHA" = "$DEPLOY_SHA"',
        'SOURCE_SHA="$DEPLOY_SHA"',
        '--source-sha "$DEPLOY_SHA"',
    ):
        assert required_contract in stage

    assert 'SOURCE_SHA="$(git rev-parse' not in stage


def test_runbook_stage_one_separates_release_controller_from_workstation() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    stage = runbook.split("## 1. Stage the exact source and image", 1)[1].split(
        "## 2. Prepare independent state", 1
    )[0]
    controller = stage.split("### Trusted release-controller context", 1)[1].split(
        "### Enterprise workstation context", 1
    )[0]
    workstation = stage.split("### Enterprise workstation context", 1)[1]

    for required_contract in (
        "git fetch --prune origin",
        "git fetch --prune github",
        'test "$ORIGIN_MAIN_SHA" = "$DEPLOY_SHA"',
        'test "$GITHUB_MAIN_SHA" = "$DEPLOY_SHA"',
        'gh run list --repo "$GITHUB_REPOSITORY" --commit "$DEPLOY_SHA"',
        "attempt,conclusion,databaseId,event,headBranch,headSha,status,url,workflowName,workflowDatabaseId",
        'GITHUB_RUNS_TMP="$(mktemp',
        '--require-workflow "Verify=${VERIFY_WORKFLOW_DATABASE_ID}@push"',
        '--require-workflow "CodeQL=${CODEQL_WORKFLOW_DATABASE_ID}@dynamic"',
        "/attempts/",
    ):
        assert required_contract in controller

    for required_contract in (
        "https://github.com/Zhang-ZhengHao/commerce-ops-desk.git",
        'STAGING_ROOT="$(mktemp -d',
        'git clone --no-checkout "$GITHUB_REPOSITORY_URL"',
        'test "$WORKSTATION_ORIGIN_MAIN_SHA" = "$DEPLOY_SHA"',
        'test "$WORKSTATION_HEAD_SHA" = "$DEPLOY_SHA"',
        "git symbolic-ref --quiet HEAD",
        "--untracked-files=all",
        "code-quarantine-",
        'mv -T -- "$CODE_ROOT"',
        'mv -T -- "$STAGING_CODE" "$CODE_ROOT"',
        "/usr/bin/python3 -I deploy/workstation/build_verified_image.py",
        "verify_exact_checkout",
    ):
        assert required_contract in workstation

    assert "gitea" not in workstation.lower()
    assert "refs/remotes/github/main" not in workstation


def test_runbook_revalidates_exact_clean_tool_identity_before_every_workstation_tool() -> (
    None
):
    runbook = RUNBOOK.read_text(encoding="utf-8")
    workstation_commands = re.findall(
        r"verify_exact_checkout\n"
        r"(?:SOURCE_SHA=.*\nAPPROVED_REMOTE_REF=.*\nBUILD_MANIFEST=.*\n)?"
        r"/usr/bin/python3 -I deploy/workstation/"
        r"(?:build_verified_image|deploy)\.py[^\n]*",
        runbook,
    )

    assert len(workstation_commands) == 6
    assert (
        re.search(
            r"/usr/bin/python3 (?!-I )deploy/workstation/(?:build_verified_image|deploy)\.py",
            runbook,
        )
        is None
    )
    assert "status --porcelain=v1 --untracked-files=all" in runbook
    assert "git symbolic-ref --quiet HEAD" in runbook
    assert "scripts/select_release_evidence.py" in runbook
    assert "deploy/workstation/build_verified_image.py" in runbook
    assert "deploy/workstation/deploy.py" in runbook


def test_runbook_safely_creates_private_deploy_state_and_quarantines_non_git_code() -> (
    None
):
    runbook = RUNBOOK.read_text(encoding="utf-8")
    workstation = runbook.split("### Enterprise workstation context", 1)[1].split(
        "## 2. Prepare independent state", 1
    )[0]

    for required_contract in (
        'if [[ ! -e "$DEPLOY_STATE" && ! -L "$DEPLOY_STATE" ]]',
        'mkdir -m 0700 -- "$DEPLOY_STATE"',
        'test ! -L "$DEPLOY_STATE"',
        'test "$(realpath -e -- "$DEPLOY_STATE")" = "$DEPLOY_STATE"',
        'test "$(/usr/bin/stat --format=%g -- "$APP_ROOT")" = "$(/usr/bin/id -g)"',
        'test "$(/usr/bin/stat --format=%a -- "$APP_ROOT")" = "700"',
        'test "$(/usr/bin/stat --format=%u -- "$DEPLOY_STATE")" = "$(/usr/bin/id -u)"',
        'test "$(/usr/bin/stat --format=%a -- "$DEPLOY_STATE")" = "700"',
        'if [[ -e "$CODE_ROOT" || -L "$CODE_ROOT" ]]',
        'CODE_QUARANTINE="$(mktemp -d',
        'mv -T -- "$CODE_ROOT" "$CODE_QUARANTINE/code"',
        'mv -T -- "$STAGING_CODE" "$CODE_ROOT"',
    ):
        assert required_contract in workstation

    quarantine_offset = workstation.index('if [[ -e "$CODE_ROOT" || -L "$CODE_ROOT" ]]')
    canonical_git_offset = workstation.index('cd -- "$CODE_ROOT"')
    assert quarantine_offset < canonical_git_offset
    assert 'git -C "$CODE_ROOT"' not in workstation[:quarantine_offset]


def test_every_runbook_bash_block_parses_with_bash_n() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", runbook, re.DOTALL)

    assert blocks
    for index, block in enumerate(blocks, start=1):
        result = subprocess.run(
            ["bash", "-n"],
            input=block,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"bash block {index}: {result.stderr}"


def test_runbook_fails_closed_and_archives_stale_candidate_inputs() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    candidate_setup = runbook.split("## 2. Prepare independent state", 1)[1].split(
        "## 3. Prepare and verify the candidate", 1
    )[0]
    normalized = " ".join(candidate_setup.split())

    for required_contract in (
        "set -euo pipefail",
        '[[ -e "$CANDIDATE_DATA_DIR" || -L "$CANDIDATE_DATA_DIR" ]]',
        "atomically creates exactly the direct child derived from the SHA",
        "fails closed",
        "quarantine archive",
        "candidate state",
        "creates a new candidate inode",
        "never delete candidate data",
        "data-only, state-only, or both",
        "If a prior run moved one item and stopped, rerun it",
        "fixed-argv",
        "dirfd",
        "O_NOFOLLOW",
        "RENAME_NOREPLACE",
        "same non-blocking per-SHA prepare lock",
        "same filesystem",
        "verify_exact_checkout",
        "/usr/bin/python3 -I deploy/workstation/deploy.py quarantine",
        '--source-sha "$INTERRUPTED_SOURCE_SHA"',
    ):
        assert required_contract in normalized

    assert 'mkdir -- "$CANDIDATE_DATA_DIR"' not in candidate_setup
    assert "sudo chown" not in candidate_setup
    assert "sudo chmod" not in candidate_setup
    assert "rm -r" not in candidate_setup
    assert "rm --" not in candidate_setup
    assert "flock --nonblock" not in candidate_setup
    assert "mv -T" not in candidate_setup


def test_runbook_selects_exact_main_ci_evidence_from_private_json() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    stage = runbook.split("## 1. Stage the exact source and image", 1)[1].split(
        "## 2. Prepare independent state", 1
    )[0]

    for required_contract in (
        'GITHUB_RUNS_TMP="$(mktemp',
        'RELEASE_EVIDENCE_TMP="$(mktemp',
        'chmod 0600 "$GITHUB_RUNS_TMP" "$RELEASE_EVIDENCE_TMP"',
        'gh run list --repo "$GITHUB_REPOSITORY" --commit "$DEPLOY_SHA"',
        "attempt,conclusion,databaseId,event,headBranch,headSha,status,url,workflowName,workflowDatabaseId",
        "scripts/select_release_evidence.py",
        "--repository Zhang-ZhengHao/commerce-ops-desk",
        '--deploy-sha "$DEPLOY_SHA"',
        '--require-workflow "Verify=${VERIFY_WORKFLOW_DATABASE_ID}@push"',
        '--require-workflow "CodeQL=${CODEQL_WORKFLOW_DATABASE_ID}@dynamic"',
        '--input "$GITHUB_RUNS_TMP"',
        "workflowDatabaseId",
        "must never be placed in argv",
        "/attempts/<attempt>",
    ):
        assert required_contract in stage

    assert "${VERIFY_WORKFLOW_DATABASE_ID:?" in stage
    assert "${CODEQL_WORKFLOW_DATABASE_ID:?" in stage
    assert re.search(r"VERIFY_WORKFLOW_DATABASE_ID=[0-9]+", stage) is None
    assert re.search(r"CODEQL_WORKFLOW_DATABASE_ID=[0-9]+", stage) is None
    assert "github-runs-${DEPLOY_SHA}.json" not in stage
    assert (
        'gh run list --repo "$GITHUB_REPOSITORY" --commit "$DEPLOY_SHA" '
        "--limit 1000 --json"
    ) in stage
    assert "--limit 1001" not in stage
    assert "999-run acceptance ceiling" in stage
    assert "1,000-result GitHub API cap" in stage


def test_runbook_lists_all_nine_external_acceptance_requirements() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    acceptance = runbook.split("## 6. External acceptance", 1)[1].split(
        "## 7. Roll back", 1
    )[0]
    normalized = " ".join(acceptance.split())

    for number in range(1, 10):
        assert re.search(rf"(?m)^{number}\. ", acceptance)
    for required_contract in (
        "independent unauthorized browser context",
        "exact full `DEPLOY_SHA`",
        "replay it without a duplicate effect",
        "observe tamper rejection",
        "clearly fictional note",
        "safe provenance plus ordered audit history",
        "Reset affects only the active synthetic tenant",
        "320-pixel mobile viewport",
        "without horizontal page overflow",
        "`/docs`, `/redoc`, and `/openapi.json`",
        "incorrect-Host",
        "controlled restart",
        "workspace persists",
        "logs contain no access code",
        "does not claim",
    ):
        assert required_contract in normalized


def test_runbook_requires_all_four_access_code_governance_confirmations() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())

    for required_contract in (
        "may be shared with external evaluators",
        "authorization scope does not unintentionally expose unrelated sites",
        "who owns revocation/rotation",
        "how an access grant is withdrawn",
        "observed gateway challenge is not a substitute",
        "private operator evaluation",
        "no access code or live-demo CTA is shared",
    ):
        assert required_contract in normalized


def test_runbook_requires_second_external_smoke_after_rollback() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    rollback = runbook.split("## 7. Roll back", 1)[1]
    normalized = " ".join(rollback.split())

    for required_contract in (
        "second external smoke",
        "public HTTPS",
        "gateway protection",
        "restored route marker",
        "old upstream identity",
        "failure path remains open",
    ):
        assert required_contract in normalized


def test_runbook_preserves_access_code_and_four_way_recovery_contracts() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    boundaries = runbook.split("## 1. Stage the exact source and image", 1)[0]
    normalized_runbook = " ".join(runbook.split())

    for prohibited_location in (
        "commands",
        "repository",
        "logs",
        "screenshots",
        "artifacts",
    ):
        assert prohibited_location in boundaries.lower()

    for recovery_branch in (
        "**Normal success:**",
        "**Normal failure with proven automatic restoration:**",
        "**Indeterminate result after the ledger path was flushed:**",
        "**Unrecoverable or externally changed state:**",
    ):
        assert runbook.count(recovery_branch) == 1
    assert "Do not retry `switch`, invoke `rollback`, or choose a different ledger" in (
        normalized_runbook
    )
    assert "rollback only with the immutable backup authorized by the current" in (
        normalized_runbook
    )
    assert "Never hand-edit Caddy, the ledger, or `active.json`" in normalized_runbook


def test_caddy_docs_scope_lock_and_legacy_migration_claims() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    design = HARDENING_DESIGN.read_text(encoding="utf-8")

    for document in (runbook, design):
        assert "cooperating invocations" in document
        assert "advisory lock" in document
        assert "cross-file atomic" in document
        assert "root writer" in document
    for required_phrase in (
        "commerce-ops-desk.v0.2.0.Caddyfile.template",
        "740ab123464b07d8e6460c974abc901c4025fc08f34a955402ba5db574994e3a",
        "schema-3 transaction",
        "active.json",
        "cannot be replayed",
    ):
        assert required_phrase in runbook
    assert "Caddy 2.6.2" in design
    assert "read-only" in design


def test_ci_parses_compose_and_caddy_with_the_workstation_version() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "docker compose -f deploy/workstation/compose.yaml config --quiet" in workflow
    )
    assert workflow.count(CADDY_LINUX_AMD64_IMAGE) == 1
    assert "caddy:2.6.2-alpine adapt" not in workflow
    assert "docker run --rm --interactive --network none" in workflow
    assert "--entrypoint caddy" in workflow
    assert "adapt --adapter caddyfile --config /dev/stdin" in workflow
    assert "commerce-ops-desk.Caddyfile.template" in workflow
    assert "commerce-ops-desk.v0.2.0.Caddyfile.template" in workflow


def test_design_summary_no_longer_calls_compose_a_future_capability() -> None:
    summary = DESIGN_SUMMARY.read_text(encoding="utf-8")

    assert "Compose deployment" not in summary
    assert "versioned workstation deployment" in summary
    assert "fail-closed blue/green" not in summary
    assert "privileged-writer boundary" in summary
