"""Live upstream identity reconstruction and drift detection contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
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

    def reject_stopped(*_args: object, **_kwargs: object) -> None:
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


def test_shared_network_peer_is_allowed_only_when_exclusivity_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    runner.network_inspection[0]["Containers"]["5" * 64] = {
        "Name": "unrelated-shared-bridge-peer",
        "EndpointID": "6" * 64,
        "MacAddress": "02:42:c0:00:02:03",
        "IPv4Address": "192.0.2.3/24",
        "IPv6Address": "",
    }

    actual = module._upstream_identity_from_inspection(
        runner,
        runner.container_inspection,
        expected_container_id=CONTAINER_ID,
        expected_container_name=CONTAINER_NAME,
        expected_host_port=HOST_PORT,
        require_exclusive_network=False,
    )

    assert actual == expected_upstream(module, runner.container_inspection)
    with pytest.raises(module.DeploymentError, match="network endpoint"):
        extract_upstream(module, runner)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("EndpointID", "7" * 64),
        ("Name", "replacement-target"),
    ],
    ids=("endpoint-id", "endpoint-name"),
)
def test_shared_network_mode_still_rejects_target_endpoint_drift(
    field: str,
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    runner.network_inspection[0]["Containers"]["5" * 64] = {
        "Name": "unrelated-shared-bridge-peer",
        "EndpointID": "6" * 64,
        "MacAddress": "02:42:c0:00:02:03",
        "IPv4Address": "192.0.2.3/24",
        "IPv6Address": "",
    }
    target_endpoint = runner.network_inspection[0]["Containers"][CONTAINER_ID]
    target_endpoint[field] = replacement

    with pytest.raises(module.DeploymentError, match="network endpoint"):
        module._upstream_identity_from_inspection(
            runner,
            runner.container_inspection,
            expected_container_id=CONTAINER_ID,
            expected_container_name=CONTAINER_NAME,
            expected_host_port=HOST_PORT,
            require_exclusive_network=False,
        )


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
    attested = module.UpstreamIdentity(
        schema=module.UPSTREAM_IDENTITY_SCHEMA,
        docker_daemon_id="6745d2ba-f78b-4baf-bf04-d1f7fc5e3e70",
        container_id=(
            "c00b0e0b0c84f24a91229329c851f4b2d4a9896d4d1b697e237722150aef8f17"
        ),
        container_name="app-commerce-ops-desk",
        image_id=(
            "sha256:92c1c424f9b1d8707a30a40b4ce086822a8104de7cec4729e010fcbc2c11062f"
        ),
        image_reference="commerce-ops-desk:0.2.0",
        source_sha="80201c4231f5c2f37ec5fd9d4cab983abbfc1eed",
        host_port=18_087,
        data_path="/home/getui/apps/commerce-ops-desk/data-v0.2.0-live",
        data_device=64_769,
        data_inode=55_451_026,
        network_name="bridge",
        network_id=("a52388fc829bf9643c7a348285b5bcb577035d76f33595b4216349b98c2d6a2a"),
        network_endpoint_id=(
            "71d12cc167f2f6637b1a5d22d9e1d08174ac797cd352952667a0b25b4a3a25bd"
        ),
        runtime_sha256=(
            "93abe2c38679f8c023bb681f68a40502b7f91b883244b11736109b351f635351"
        ),
    )
    expected_fingerprint = (
        "892810d70dc768db1db79f84276fd6df9b3e83085e34fe8089b604532db11e89"
    )

    assert module._upstream_identity_fingerprint(attested) == expected_fingerprint
    assert module.LEGACY_UPSTREAM_FINGERPRINT == expected_fingerprint


def test_host_port_discovery_ignores_stopped_binding_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()

    class DiscoveryRunner(FakeRunner):
        def __init__(self, loaded_module: ModuleType) -> None:
            super().__init__(loaded_module)
            self.stopped_id = "5" * 64
            self.stopped_inspection = deepcopy(self.container_inspection)
            self.stopped_inspection["Id"] = self.stopped_id
            self.stopped_inspection["Name"] = "/stopped-evidence"
            self.stopped_inspection["State"] = {
                "Status": "exited",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "Health": {"Status": "healthy"},
            }

        def run(
            self, arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            running_command = [
                self.module.DOCKER_BINARY,
                "container",
                "ls",
                "--quiet",
                "--no-trunc",
            ]
            all_command = [
                self.module.DOCKER_BINARY,
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
            ]
            if command in (running_command, all_command):
                self.calls.append(command)
                identifiers = (
                    [self.stopped_id, CONTAINER_ID]
                    if command == all_command
                    else [CONTAINER_ID]
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "\n".join(identifiers) + "\n",
                    "",
                )
            if command == [self.module.DOCKER_BINARY, "inspect", self.stopped_id]:
                self.calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps([self.stopped_inspection]),
                    "",
                )
            return super().run(command, **kwargs)

    runner = DiscoveryRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)

    actual = module._upstream_identity_for_host_port(runner, HOST_PORT)

    assert actual == expected_upstream(module, runner.container_inspection)
    assert runner.calls[0] == [
        module.DOCKER_BINARY,
        "container",
        "ls",
        "--quiet",
        "--no-trunc",
    ]


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
            "--quiet",
            "--no-trunc",
        ]
    ]


@pytest.mark.parametrize(
    ("profile", "expected_exclusive"),
    [
        ("legacy-v0.2.0", False),
        ("hardened-v1", True),
    ],
)
def test_route_readiness_selects_network_policy_from_profile(
    profile: str,
    expected_exclusive: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    upstream = expected_upstream(module, runner.container_inspection)
    route = module.RouteState(
        fragment_sha256="a" * 64,
        profile=profile,
        route_revision=None if profile == "legacy-v0.2.0" else "b" * 64,
        upstream=upstream,
        deployment_assets=None,
    )
    observed: list[bool] = []

    def capture_policy(
        _runner: object,
        received: object,
        *,
        require_exclusive_network: bool,
    ) -> None:
        assert received is upstream
        observed.append(require_exclusive_network)

    monkeypatch.setattr(module, "_assert_upstream_ready", capture_policy)

    module._assert_route_upstream_ready(object(), route)

    assert observed == [expected_exclusive]


@pytest.mark.parametrize(
    ("profile", "shared_network_allowed"),
    [
        ("legacy-v0.2.0", True),
        ("hardened-v1", False),
    ],
)
def test_route_readiness_propagates_policy_through_identity_verification(
    profile: str,
    shared_network_allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    install_fake_data_stat(module, runner, monkeypatch)
    upstream = expected_upstream(module, runner.container_inspection)
    route = module.RouteState(
        fragment_sha256="a" * 64,
        profile=profile,
        route_revision=None if profile == "legacy-v0.2.0" else "b" * 64,
        upstream=upstream,
        deployment_assets=None,
    )
    runner.network_inspection[0]["Containers"]["5" * 64] = {
        "Name": "unrelated-shared-bridge-peer",
        "EndpointID": "6" * 64,
        "MacAddress": "02:42:c0:00:02:03",
        "IPv4Address": "192.0.2.3/24",
        "IPv6Address": "",
    }

    def assert_healthy(
        _runner: object,
        container_name: str,
        *,
        timeout: float,
    ) -> None:
        assert container_name == CONTAINER_NAME
        assert timeout == 15.0

    monkeypatch.setattr(module, "_wait_for_healthy", assert_healthy)

    if shared_network_allowed:
        module._assert_route_upstream_ready(runner, route)
    else:
        with pytest.raises(module.DeploymentError, match="network endpoint"):
            module._assert_route_upstream_ready(runner, route)


def test_route_readiness_rejects_unknown_network_policy_profile() -> None:
    module = load_deploy_tool()
    runner = FakeRunner(module)
    route = module.RouteState(
        fragment_sha256="a" * 64,
        profile="unmanaged-profile",
        route_revision=None,
        upstream=expected_upstream(module, runner.container_inspection),
        deployment_assets=None,
    )

    with pytest.raises(module.DeploymentError, match="profile"):
        module._assert_route_upstream_ready(object(), route)


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
