"""Static contracts for the versioned workstation deployment assets."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
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
        "--no-deps",
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
        "--no-deps",
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


def test_prepare_uses_one_verified_compose_service_and_records_final_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_deploy_tool()
    product_root = tmp_path / "product"
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
    monkeypatch.setattr(module, "_load_build_manifest", lambda _path: manifest)
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
    monkeypatch.setattr(
        module,
        "_write_private_json",
        lambda _path, payload: written.update(payload),
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
    assert [call[0][-2:] for call in compose_calls] == [
        ["config", "--services"],
        ["--no-deps", "commerce-ops-desk"],
        ["--no-deps", "commerce-ops-desk"],
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
        "sudo chown 10001:10001",
        "sudo chmod 0700",
    ):
        assert required_phrase in runbook
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
