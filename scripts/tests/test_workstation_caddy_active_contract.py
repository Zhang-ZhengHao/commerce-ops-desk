"""Contracts for observing the active Caddy configuration safely."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
ADMIN_CONFIG_COMMAND = [
    "/usr/bin/curl",
    "--disable",
    "--silent",
    "--show-error",
    "--fail",
    "--connect-timeout",
    "2",
    "--max-time",
    "5",
    "--max-filesize",
    "4194304",
    "--noproxy",
    "*",
    "http://127.0.0.1:2019/config/",
]


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_caddy_active_contract", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def host_route(host: str, port: int) -> dict[str, object]:
    return {
        "match": [{"host": [host]}],
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": f"127.0.0.1:{port}"}],
                            }
                        ]
                    }
                ],
            }
        ],
        "terminal": True,
    }


def caddy_document(
    servers: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {"apps": {"http": {"servers": servers}}}


class ActiveConfigRunner:
    """Serve deterministic admin and adapt responses while recording argv."""

    def __init__(
        self,
        *,
        active: object,
        adapted: dict[str, object] | None = None,
        active_text: str | None = None,
    ) -> None:
        self.active = active
        self.adapted = adapted
        self.active_text = active_text
        self.calls: list[tuple[list[str], str | None]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append((command, input_text))
        if command == ADMIN_CONFIG_COMMAND:
            output = (
                self.active_text
                if self.active_text is not None
                else json.dumps(self.active)
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == [
            "/usr/bin/caddy",
            "adapt",
            "--adapter",
            "caddyfile",
            "--config",
            "/dev/stdin",
        ]:
            assert input_text is not None
            assert self.adapted is not None
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(self.adapted),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")


def test_extract_managed_route_finds_one_exact_host_across_all_servers() -> None:
    module = load_deploy_tool()
    managed = host_route(module.CADDY_HOST, 18088)
    document = caddy_document(
        {
            "unrelated-before": {
                "routes": [
                    host_route("other.example", 19001),
                    host_route(module.CADDY_HOST, 19002)
                    | {"match": [{"host": [module.CADDY_HOST, "attacker.example"]}]},
                ]
            },
            "managed-at-an-arbitrary-key": {"routes": [managed]},
            "unrelated-after": {"routes": [host_route("last.example", 19003)]},
        }
    )

    assert module._extract_managed_caddy_route(document) == managed


def test_extract_managed_route_rejects_duplicate_exact_host_routes() -> None:
    module = load_deploy_tool()
    document = caddy_document(
        {
            "server-a": {"routes": [host_route(module.CADDY_HOST, 18087)]},
            "server-b": {"routes": [host_route(module.CADDY_HOST, 18088)]},
        }
    )

    with pytest.raises(module.DeploymentError):
        module._extract_managed_caddy_route(document)


def test_assert_active_route_accepts_the_exact_adapted_host_route() -> None:
    module = load_deploy_tool()
    fragment = f"http://{module.CADDY_HOST} {{\n\treverse_proxy 127.0.0.1:18088\n}}\n"
    expected = host_route(module.CADDY_HOST, 18088)
    active = caddy_document(
        {
            "unrelated": {"routes": [host_route("other.example", 19001)]},
            "managed": {"routes": [expected]},
        }
    )
    adapted = caddy_document({"fragment": {"routes": [expected]}})
    runner = ActiveConfigRunner(active=active, adapted=adapted)

    module._assert_active_caddy_route(runner, fragment)

    adapt_call = (
        [
            "/usr/bin/caddy",
            "adapt",
            "--adapter",
            "caddyfile",
            "--config",
            "/dev/stdin",
        ],
        fragment,
    )
    assert len(runner.calls) == 2
    assert runner.calls.count((ADMIN_CONFIG_COMMAND, None)) == 1
    assert runner.calls.count(adapt_call) == 1


def test_assert_active_route_rejects_a_route_that_differs_from_the_fragment() -> None:
    module = load_deploy_tool()
    fragment = f"http://{module.CADDY_HOST} {{\n\treverse_proxy 127.0.0.1:18088\n}}\n"
    active = caddy_document(
        {"managed": {"routes": [host_route(module.CADDY_HOST, 18087)]}}
    )
    adapted = caddy_document(
        {"fragment": {"routes": [host_route(module.CADDY_HOST, 18088)]}}
    )
    runner = ActiveConfigRunner(active=active, adapted=adapted)

    with pytest.raises(module.DeploymentError):
        module._assert_active_caddy_route(runner, fragment)


def test_active_config_uses_the_fixed_local_admin_request() -> None:
    module = load_deploy_tool()
    document = caddy_document({})
    runner = ActiveConfigRunner(active=document)

    assert module._active_caddy_config(runner) == document
    assert runner.calls == [(ADMIN_CONFIG_COMMAND, None)]


@pytest.mark.parametrize("active_text", ["not-json", "[]", "null"])
def test_active_config_rejects_non_document_responses(active_text: str) -> None:
    module = load_deploy_tool()
    runner = ActiveConfigRunner(active={}, active_text=active_text)

    with pytest.raises(module.DeploymentError):
        module._active_caddy_config(runner)


def test_caddy_loopback_ports_recurses_through_static_reverse_proxies() -> None:
    module = load_deploy_tool()
    document = {
        "apps": {
            "http": {
                "servers": {
                    "nested": {
                        "routes": [
                            {
                                "handle": [
                                    {
                                        "handler": "subroute",
                                        "routes": [
                                            {
                                                "handle": [
                                                    {
                                                        "handler": "reverse_proxy",
                                                        "upstreams": [
                                                            {
                                                                "dial": (
                                                                    "127.0.0.1:18081"
                                                                )
                                                            },
                                                            {
                                                                "dial": (
                                                                    "127.42.7.9:18082"
                                                                )
                                                            },
                                                            {
                                                                "dial": (
                                                                    "localhost:18083"
                                                                )
                                                            },
                                                            {"dial": "[::1]:18084"},
                                                            {
                                                                "dial": (
                                                                    "192.0.2.10:19001"
                                                                )
                                                            },
                                                        ],
                                                    }
                                                ]
                                            }
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        },
        "unrelated": {"dial": "127.0.0.1:19999"},
    }

    assert module._caddy_loopback_ports(document) == {
        18081,
        18082,
        18083,
        18084,
    }


@pytest.mark.parametrize(
    "reverse_proxy",
    [
        {
            "handler": "reverse_proxy",
            "dynamic_upstreams": {"source": "srv", "service": "backend"},
        },
        {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": "127.0.0.1:{env.UPSTREAM_PORT}"}],
        },
        {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": "127.0.0.1:18080-18090"}],
        },
    ],
    ids=["dynamic-upstream", "placeholder", "port-range"],
)
def test_caddy_loopback_ports_fails_closed_for_non_static_upstreams(
    reverse_proxy: dict[str, object],
) -> None:
    module = load_deploy_tool()
    document = caddy_document({"server": {"routes": [{"handle": [reverse_proxy]}]}})

    with pytest.raises(module.DeploymentError):
        module._caddy_loopback_ports(document)


def test_caddy_port_documents_use_both_disk_and_active_structured_configs() -> None:
    module = load_deploy_tool()
    disk_document = caddy_document(
        {"disk": {"routes": [host_route("disk.example", 18088)]}}
    )
    active_document = caddy_document(
        {"active": {"routes": [host_route("active.example", 18089)]}}
    )
    calls: list[list[str]] = []

    class PortRunner:
        def run(
            self, arguments: Sequence[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            calls.append(command)
            if command == [
                module.SUDO_BINARY,
                module.CADDY_BINARY,
                "adapt",
                "--adapter",
                "caddyfile",
                "--config",
                str(module.CADDY_MAIN_CONFIG),
            ]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(disk_document), ""
                )
            if command == ADMIN_CONFIG_COMMAND:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(active_document), ""
                )
            raise AssertionError(f"unexpected command: {command}")

    assert module._caddy_port_documents(PortRunner(), 18088) == {
        "on-disk Caddy configuration": "127.0.0.1:18088"
    }
    assert calls == [
        [
            module.SUDO_BINARY,
            module.CADDY_BINARY,
            "adapt",
            "--adapter",
            "caddyfile",
            "--config",
            str(module.CADDY_MAIN_CONFIG),
        ],
        ADMIN_CONFIG_COMMAND,
    ]
