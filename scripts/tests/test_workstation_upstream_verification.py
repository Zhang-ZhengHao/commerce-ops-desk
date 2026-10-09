"""Live upstream identity reconstruction and drift detection contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
CONTAINER_ID = "1" * 64
CONTAINER_NAME = "app-commerce-ops-desk-candidate-0123456789ab"
IMAGE_ID = "sha256:" + "2" * 64
IMAGE_REFERENCE = f"commerce-ops-desk:{SOURCE_SHA}"
HOST_PORT = 18_088
DATA_PATH = "/srv/commerce-ops-desk/data-candidate-0123456789ab"
DATA_IDENTITY = (64_769, 55_451_027, 10_001, 10_001)
NETWORK_NAME = "commerce-ops-candidate-0123456789ab_default"
NETWORK_ID = "3" * 64
ENDPOINT_ID = "4" * 64
DOCKER_DAEMON_ID = "synthetic-local-daemon"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_upstream_verification", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_container_inspection() -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "Name": f"/{CONTAINER_NAME}",
        "Image": IMAGE_ID,
        "Path": "/opt/venv/bin/python",
        "Args": ["-m", "uvicorn", "app.main:app"],
        "Config": {
            "Image": IMAGE_REFERENCE,
            "User": "10001:10001",
            "Labels": {
                "org.opencontainers.image.revision": SOURCE_SHA,
                "com.docker.compose.project": ("commerce-ops-candidate-0123456789ab"),
                "com.docker.compose.service": "commerce-ops-desk",
            },
        },
        "HostConfig": {
            "PortBindings": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(HOST_PORT)}]
            }
        },
        "State": {
            "Status": "running",
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "Health": {"Status": "healthy"},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": DATA_PATH,
                "Destination": "/app/data",
                "Mode": "",
                "Propagation": "rprivate",
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Networks": {
                NETWORK_NAME: {
                    "NetworkID": NETWORK_ID,
                    "EndpointID": ENDPOINT_ID,
                    "Gateway": "192.0.2.1",
                }
            }
        },
        "AppArmorProfile": "docker-default",
        "Driver": "overlay2",
        "Platform": "linux",
        "ProcessLabel": "",
        "MountLabel": "",
    }


def valid_network_inspection() -> list[dict[str, Any]]:
    return [
        {
            "Name": NETWORK_NAME,
            "Id": NETWORK_ID,
            "Created": "2026-10-09T00:00:00.000000000Z",
            "Scope": "local",
            "Driver": "bridge",
            "EnableIPv4": True,
            "EnableIPv6": False,
            "IPAM": {
                "Driver": "default",
                "Options": None,
                "Config": [{"Subnet": "192.0.2.0/24", "Gateway": "192.0.2.1"}],
            },
            "Internal": False,
            "Attachable": False,
            "Ingress": False,
            "ConfigFrom": {"Network": ""},
            "ConfigOnly": False,
            "Containers": {
                CONTAINER_ID: {
                    "Name": CONTAINER_NAME,
                    "EndpointID": ENDPOINT_ID,
                    "MacAddress": "02:42:c0:00:02:02",
                    "IPv4Address": "192.0.2.2/24",
                    "IPv6Address": "",
                }
            },
            "Options": {},
            "Labels": {
                "com.docker.compose.config-hash": "5" * 64,
                "com.docker.compose.network": "default",
                "com.docker.compose.project": ("commerce-ops-candidate-0123456789ab"),
                "com.docker.compose.version": "2.40.3",
            },
        }
    ]


class FakeRunner:
    def __init__(self, module: ModuleType) -> None:
        self.module = module
        self.daemon_id = DOCKER_DAEMON_ID
        self.container_inspection = valid_container_inspection()
        self.network_inspection = valid_network_inspection()
        self.host_data_identity = DATA_IDENTITY
        self.container_data_identity = DATA_IDENTITY
        self.calls: list[list[str]] = []

    def run(
        self, arguments: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append(command)
        if command == [
            self.module.DOCKER_BINARY,
            "info",
            "--format",
            "{{json .ID}}",
        ]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.daemon_id), ""
            )
        if (
            len(command) == 3
            and command[0] == self.module.DOCKER_BINARY
            and command[1] in {"inspect", "container"}
        ):
            return subprocess.CompletedProcess(
                command, 0, json.dumps([self.container_inspection]), ""
            )
        if len(command) == 4 and command[:3] == [
            self.module.DOCKER_BINARY,
            "container",
            "inspect",
        ]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps([self.container_inspection]), ""
            )
        if command[:3] == [
            self.module.DOCKER_BINARY,
            "network",
            "inspect",
        ]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.network_inspection), ""
            )
        if command[:2] == [self.module.DOCKER_BINARY, "exec"]:
            output = "|".join(str(value) for value in self.container_data_identity)
            return subprocess.CompletedProcess(command, 0, output + "\n", "")
        raise AssertionError(f"unexpected command: {command!r}")


def expected_upstream(module: ModuleType, inspection: dict[str, Any]) -> object:
    return module.UpstreamIdentity(
        schema=module.UPSTREAM_IDENTITY_SCHEMA,
        docker_daemon_id=DOCKER_DAEMON_ID,
        container_id=CONTAINER_ID,
        container_name=CONTAINER_NAME,
        image_id=IMAGE_ID,
        image_reference=IMAGE_REFERENCE,
        source_sha=SOURCE_SHA,
        host_port=HOST_PORT,
        data_path=DATA_PATH,
        data_device=DATA_IDENTITY[0],
        data_inode=DATA_IDENTITY[1],
        network_name=NETWORK_NAME,
        network_id=NETWORK_ID,
        network_endpoint_id=ENDPOINT_ID,
        runtime_sha256=module._candidate_runtime_sha256(inspection),
    )


def install_fake_data_stat(
    module: ModuleType,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "_data_directory_identity",
        lambda _path: runner.host_data_identity,
    )


def extract_upstream(module: ModuleType, runner: FakeRunner) -> object:
    return module._upstream_identity_from_inspection(
        runner,
        runner.container_inspection,
        expected_container_id=CONTAINER_ID,
        expected_container_name=CONTAINER_NAME,
        expected_host_port=HOST_PORT,
    )


def test_upstream_identity_is_reconstructed_from_independent_live_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)

    actual = extract_upstream(module, runner)

    assert actual == expected_upstream(module, runner.container_inspection)
    assert [module.DOCKER_BINARY, "info", "--format", "{{json .ID}}"] in (runner.calls)
    assert [module.DOCKER_BINARY, "network", "inspect", NETWORK_NAME] in (runner.calls)
    assert any(
        command[:2] == [module.DOCKER_BINARY, "exec"] for command in runner.calls
    )


def test_current_upstream_accepts_the_exact_recorded_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    expected = expected_upstream(module, runner.container_inspection)

    module._assert_upstream_identity_current(runner, expected)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Status", "exited"),
        ("Running", False),
        ("Paused", True),
        ("Restarting", True),
        ("Dead", True),
        ("Health", {"Status": "unhealthy"}),
    ],
)
def test_upstream_identity_rejects_a_nonrunning_or_unhealthy_container(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    runner.container_inspection["State"][field] = value

    with pytest.raises(module.DeploymentError, match="running|healthy|state"):
        extract_upstream(module, runner)


def test_health_wait_rejects_stale_healthy_metadata_on_a_stopped_container() -> None:
    module = load_deploy_tool()
    stale_state = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "Health": {"Status": "healthy"},
    }

    class StateRunner:
        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(stale_state),
                "",
            )

    with pytest.raises(module.DeploymentError, match="exited|running"):
        module._wait_for_healthy(StateRunner(), CONTAINER_NAME, timeout=1.0)


def test_loaded_route_rechecks_health_after_the_smoke_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    route = module.RouteState(
        fragment_sha256="a" * 64,
        profile=module.CADDY_PROFILE_HARDENED,
        route_revision="b" * 64,
        upstream=expected_upstream(module, runner.container_inspection),
        deployment_assets=None,
    )
    monkeypatch.setattr(module, "_validate_caddy", lambda _runner: None)
    monkeypatch.setattr(module, "_reload_caddy", lambda _runner: None)
    monkeypatch.setattr(
        module,
        "_assert_active_caddy_route",
        lambda _runner, _fragment: None,
    )
    monkeypatch.setattr(module, "_smoke_caddy", lambda _route: None)
    monkeypatch.setattr(
        module,
        "_assert_upstream_identity_current",
        lambda *_args: pytest.fail("identity-only final check is insufficient"),
    )

    def reject_stopped(*_args: object) -> None:
        raise module.DeploymentError("upstream is no longer running")

    monkeypatch.setattr(module, "_assert_upstream_ready", reject_stopped)

    with pytest.raises(module.DeploymentError, match="no longer running"):
        module._verify_loaded_caddy_route(runner, "fragment\n", route)


@pytest.mark.parametrize(
    "mutation",
    [
        "docker-daemon",
        "container-id",
        "container-name",
        "image-id",
        "image-reference",
        "source-sha",
        "host-port",
        "data-path",
        "data-device",
        "data-inode",
        "container-data-stat",
        "network-name",
        "network-id",
        "network-endpoint-id",
        "runtime-sha256",
    ],
)
def test_current_upstream_rejects_every_recorded_identity_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    expected = expected_upstream(module, runner.container_inspection)

    if mutation == "docker-daemon":
        runner.daemon_id = "replacement-daemon"
    elif mutation == "container-id":
        replacement = "a" * 64
        runner.container_inspection["Id"] = replacement
        endpoint = runner.network_inspection[0]["Containers"].pop(CONTAINER_ID)
        runner.network_inspection[0]["Containers"][replacement] = endpoint
    elif mutation == "container-name":
        runner.container_inspection["Name"] = "/replacement-container"
        endpoint = runner.network_inspection[0]["Containers"][CONTAINER_ID]
        endpoint["Name"] = "replacement-container"
    elif mutation == "image-id":
        runner.container_inspection["Image"] = "sha256:" + "a" * 64
    elif mutation == "image-reference":
        runner.container_inspection["Config"]["Image"] = "commerce-ops-desk:other"
    elif mutation == "source-sha":
        labels = runner.container_inspection["Config"]["Labels"]
        labels["org.opencontainers.image.revision"] = "a" * 40
    elif mutation == "host-port":
        binding = runner.container_inspection["HostConfig"]["PortBindings"]
        binding["8000/tcp"][0]["HostPort"] = "18089"
    elif mutation == "data-path":
        runner.container_inspection["Mounts"][0]["Source"] = (
            "/srv/commerce-ops-desk/replacement-data"
        )
    elif mutation == "data-device":
        runner.host_data_identity = (64_770, *DATA_IDENTITY[1:])
        runner.container_data_identity = runner.host_data_identity
    elif mutation == "data-inode":
        runner.host_data_identity = (
            DATA_IDENTITY[0],
            55_451_028,
            *DATA_IDENTITY[2:],
        )
        runner.container_data_identity = runner.host_data_identity
    elif mutation == "container-data-stat":
        runner.container_data_identity = (
            DATA_IDENTITY[0],
            55_451_028,
            *DATA_IDENTITY[2:],
        )
    elif mutation == "network-name":
        current_network = runner.container_inspection["NetworkSettings"]["Networks"]
        endpoint = current_network.pop(NETWORK_NAME)
        current_network["replacement-network"] = endpoint
        runner.network_inspection[0]["Name"] = "replacement-network"
    elif mutation == "network-id":
        replacement = "a" * 64
        network = runner.container_inspection["NetworkSettings"]["Networks"]
        network[NETWORK_NAME]["NetworkID"] = replacement
        runner.network_inspection[0]["Id"] = replacement
    elif mutation == "network-endpoint-id":
        replacement = "b" * 64
        network = runner.container_inspection["NetworkSettings"]["Networks"]
        network[NETWORK_NAME]["EndpointID"] = replacement
        endpoint = runner.network_inspection[0]["Containers"][CONTAINER_ID]
        endpoint["EndpointID"] = replacement
    else:
        assert mutation == "runtime-sha256"
        runner.container_inspection["Args"].append("--replacement-runtime")

    with pytest.raises(module.DeploymentError, match="upstream|container|data|network"):
        module._assert_upstream_identity_current(runner, expected)


@pytest.mark.parametrize("mutation", ["public-bind", "second-bind"])
def test_upstream_extraction_requires_one_loopback_port_binding(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    bindings = runner.container_inspection["HostConfig"]["PortBindings"]
    if mutation == "public-bind":
        bindings["8000/tcp"][0]["HostIp"] = "0.0.0.0"
    else:
        bindings["9000/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "19000"}]

    with pytest.raises(module.DeploymentError, match="port|loopback"):
        extract_upstream(module, runner)


@pytest.mark.parametrize("mutation", ["network-id", "endpoint-id"])
def test_upstream_extraction_rejects_disagreement_between_docker_views(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    if mutation == "network-id":
        runner.network_inspection[0]["Id"] = "a" * 64
    else:
        endpoint = runner.network_inspection[0]["Containers"][CONTAINER_ID]
        endpoint["EndpointID"] = "b" * 64

    with pytest.raises(module.DeploymentError, match="network"):
        extract_upstream(module, runner)


def test_upstream_extraction_rejects_container_and_host_data_stat_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    runner.container_data_identity = (
        DATA_IDENTITY[0],
        55_451_028,
        *DATA_IDENTITY[2:],
    )

    with pytest.raises(module.DeploymentError, match="data"):
        extract_upstream(module, runner)


def test_frozen_legacy_upstream_fingerprint_matches_read_only_evidence() -> None:
    module = load_deploy_tool()

    assert module.LEGACY_UPSTREAM_FINGERPRINT == (
        "b443797953fc44f8acd0e8a353bec2853d4a100fdf113a48d2815754ce4c30ac"
    )


def test_host_port_discovery_requests_full_ids_and_rejects_truncated_output() -> None:
    module = load_deploy_tool()

    class DiscoveryRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "1" * 12 + "\n", "")

    runner = DiscoveryRunner()

    with pytest.raises(module.DeploymentError, match="invalid container identity"):
        module._upstream_identity_for_host_port(runner, HOST_PORT)

    assert runner.calls == [
        [
            module.DOCKER_BINARY,
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
        ]
    ]


def test_synthetic_upstream_fingerprint_binds_reconstructed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    expected = expected_upstream(module, runner.container_inspection)

    assert module._upstream_identity_fingerprint(extract_upstream(module, runner)) == (
        module._upstream_identity_fingerprint(expected)
    )
