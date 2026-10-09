"""Fail-closed Docker network contracts for workstation candidates."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
CONTAINER_ID = "b" * 64


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_network_contract", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_network_inspection(
    module: ModuleType,
    *,
    running: bool,
) -> list[dict[str, object]]:
    identity = module.candidate_identity(VALID_SHA)
    containers: dict[str, object] = {}
    if running:
        containers[CONTAINER_ID] = {
            "Name": identity.container_name,
            "EndpointID": "c" * 64,
            "MacAddress": "02:42:ac:1e:00:02",
            "IPv4Address": "192.0.2.2/24",
            "IPv6Address": "",
        }
    return [
        {
            "Name": f"{identity.project_name}_default",
            "Id": "a" * 64,
            "Created": "2026-10-09T00:00:00.000000000Z",
            "Scope": "local",
            "Driver": "bridge",
            "EnableIPv4": True,
            "EnableIPv6": False,
            "IPAM": {
                "Driver": "default",
                "Options": None,
                "Config": [
                    {
                        "Subnet": "192.0.2.0/24",
                        "Gateway": "192.0.2.1",
                    }
                ],
            },
            "Internal": False,
            "Attachable": False,
            "Ingress": False,
            "ConfigFrom": {"Network": ""},
            "ConfigOnly": False,
            "Containers": containers,
            "Options": {},
            "Labels": {
                "com.docker.compose.config-hash": "d" * 64,
                "com.docker.compose.network": "default",
                "com.docker.compose.project": identity.project_name,
                "com.docker.compose.version": "2.40.3",
            },
        }
    ]


def validate_network(
    module: ModuleType,
    inspection: object,
    *,
    running: bool = True,
) -> str:
    identity = module.candidate_identity(VALID_SHA)
    return module._assert_candidate_network(
        identity,
        f"{identity.project_name}_default",
        inspection,
        expected_container_id=CONTAINER_ID if running else None,
    )


def test_candidate_network_accepts_the_exact_stopped_and_running_contracts() -> None:
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    network_name = f"{identity.project_name}_default"

    assert (
        validate_network(
            module,
            valid_network_inspection(module, running=False),
            running=False,
        )
        == '["192.0.2.1/32"]'
    )
    assert (
        validate_network(
            module,
            valid_network_inspection(module, running=True),
        )
        == '["192.0.2.1/32"]'
    )
    running_contract = module._validate_candidate_network_contract(
        identity,
        network_name,
        valid_network_inspection(module, running=True),
        expected_container_id=CONTAINER_ID,
    )
    assert running_contract.network_id == "a" * 64
    assert running_contract.endpoint_id == "c" * 64


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("Scope", "swarm"),
        ("Driver", "macvlan"),
        ("EnableIPv4", False),
        ("EnableIPv6", True),
        ("Internal", True),
        ("Attachable", True),
        ("Ingress", True),
        ("ConfigFrom", {"Network": "attacker"}),
        ("ConfigOnly", True),
        ("Options", {"com.docker.network.bridge.enable_icc": "true"}),
        ("FutureNetworkEscape", True),
    ],
)
def test_candidate_network_rejects_unapproved_top_level_settings(
    field: str,
    unsafe_value: object,
) -> None:
    module = load_deploy_tool()
    inspection = valid_network_inspection(module, running=True)
    inspection[0][field] = copy.deepcopy(unsafe_value)

    with pytest.raises(module.DeploymentError, match="network contract"):
        validate_network(module, inspection)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("Driver", "custom"),
        ("Options", {"parent": "eth0"}),
        (
            "Config",
            [
                {"Subnet": "192.0.2.0/24", "Gateway": "192.0.2.1"},
                {"Subnet": "198.51.100.0/24", "Gateway": "198.51.100.1"},
            ],
        ),
        (
            "Config",
            [{"Subnet": "2001:db8::/64", "Gateway": "2001:db8::1"}],
        ),
    ],
)
def test_candidate_network_rejects_unapproved_ipam(
    field: str,
    unsafe_value: object,
) -> None:
    module = load_deploy_tool()
    inspection = valid_network_inspection(module, running=True)
    ipam = inspection[0]["IPAM"]
    assert isinstance(ipam, dict)
    ipam[field] = copy.deepcopy(unsafe_value)

    with pytest.raises(module.DeploymentError, match="network contract"):
        validate_network(module, inspection)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("EndpointID", ""),
        ("MacAddress", ""),
        ("IPv4Address", "198.51.100.2/24"),
        ("IPv4Address", "192.0.2.1/24"),
        ("IPv6Address", "2001:db8::2/64"),
        ("FutureEndpointEscape", True),
    ],
)
def test_candidate_network_rejects_unapproved_endpoint_identity(
    field: str,
    unsafe_value: object,
) -> None:
    module = load_deploy_tool()
    inspection = valid_network_inspection(module, running=True)
    containers = inspection[0]["Containers"]
    assert isinstance(containers, dict)
    endpoint = containers[CONTAINER_ID]
    assert isinstance(endpoint, dict)
    endpoint[field] = copy.deepcopy(unsafe_value)

    with pytest.raises(module.DeploymentError, match="network endpoint"):
        validate_network(module, inspection)
