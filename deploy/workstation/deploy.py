"""Scoped blue/green deployment helper for the shared workstation.

Run this file on the workstation from the exact source revision being deployed.
It never accepts an access code or application secret and it only installs the
CommerceOps Caddy site fragment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    ip_address,
    ip_interface,
    ip_network,
)
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PRODUCT_ROOT / "deploy" / "workstation" / "compose.yaml"
CADDY_TEMPLATE = (
    PRODUCT_ROOT / "deploy" / "workstation" / "commerce-ops-desk.Caddyfile.template"
)
LEGACY_CADDY_TEMPLATE = (
    PRODUCT_ROOT
    / "deploy"
    / "workstation"
    / "commerce-ops-desk.v0.2.0.Caddyfile.template"
)
APP_ROOT = Path.home() / "apps" / "commerce-ops-desk"
STATE_DIRECTORY = APP_ROOT / "deploy-state"
CADDY_SITE = Path("/etc/caddy/sites/commerce-ops-desk.conf")
CADDY_SITES_DIRECTORY = CADDY_SITE.parent
CADDY_MAIN_CONFIG = Path("/etc/caddy/Caddyfile")
CADDY_TRANSACTION_LOCK = Path("/run/lock/commerce-ops-desk-caddy.lock")
CADDY_MUTATION_LOCK = Path("/run/lock/commerce-ops-desk-caddy-mutation.lock")
CADDY_MUTATION_FENCE = Path("/run/lock/commerce-ops-desk-caddy-mutation.fence")
CADDY_TRANSACTION_DIRECTORY = Path("/var/lib/commerce-ops-desk/caddy-transactions")
CADDY_ACTIVE_STATE = CADDY_TRANSACTION_DIRECTORY / "active.json"
CADDY_HOST = "commerce-ops-desk.srrsh.aig.rest"
CADDY_PORT_PLACEHOLDER = "{{UPSTREAM_PORT}}"
CADDY_REVISION_PLACEHOLDER = "{{ROUTE_REVISION}}"
CADDY_REVISION_HEADER = "X-CommerceOps-Route-Revision"
CADDY_PROFILE_HARDENED = "hardened-v1"
CADDY_PROFILE_LEGACY_V020 = "legacy-v0.2.0"
LEGACY_CADDY_PORT = 18_087
LEGACY_CADDY_FRAGMENT_SHA256 = (
    "740ab123464b07d8e6460c974abc901c4025fc08f34a955402ba5db574994e3a"
)
CADDY_PROFILE_TEMPLATES = {
    CADDY_PROFILE_HARDENED: CADDY_TEMPLATE,
    CADDY_PROFILE_LEGACY_V020: LEGACY_CADDY_TEMPLATE,
}
CADDY_MANAGED_PROFILES = frozenset(CADDY_PROFILE_TEMPLATES)
RUNTIME_UID = 10_001
RUNTIME_GID = 10_001
HEALTH_TIMEOUT_SECONDS = 120.0
COMMAND_TIMEOUT_SECONDS = 60.0
DOCKER_BINARY = "/usr/bin/docker"
CADDY_BINARY = "/usr/bin/caddy"
CURL_BINARY = "/usr/bin/curl"
SUDO_BINARY = "/usr/bin/sudo"
SS_BINARY = "/usr/bin/ss"
SYSTEMCTL_BINARY = "/usr/bin/systemctl"
PYTHON_BINARY = "/usr/bin/python3"
SAFE_SYSTEM_PATH = "/usr/bin:/bin"
SAFE_PRIVILEGED_ENVIRONMENT = MappingProxyType(
    {
        "PATH": SAFE_SYSTEM_PATH,
        "LC_ALL": "C",
        "LANG": "C",
    }
)
SAFE_SUDO_SUBCOMMANDS = MappingProxyType(
    {
        "cat": "/usr/bin/cat",
        "install": "/usr/bin/install",
        "ln": "/usr/bin/ln",
        "rm": "/usr/bin/rm",
        "stat": "/usr/bin/stat",
        "test": "/usr/bin/test",
        "true": "/usr/bin/true",
    }
)
CADDY_MUTATION_FENCE_PRELUDE = """\
import fcntl
import os
import stat
import sys

fence_token = sys.argv[-3]
mutation_lock_path = sys.argv[-2]
fence_state_path = sys.argv[-1]
if (
    len(fence_token) != 64
    or any(character not in "0123456789abcdef" for character in fence_token)
    or os.path.dirname(mutation_lock_path) != os.path.dirname(fence_state_path)
):
    raise SystemExit("invalid Caddy mutation fence arguments")
lock_stat = os.lstat(mutation_lock_path)
if (
    not stat.S_ISREG(lock_stat.st_mode)
    or lock_stat.st_uid != 0
    or lock_stat.st_gid != 0
    or stat.S_IMODE(lock_stat.st_mode) != 0o600
    or lock_stat.st_nlink != 1
):
    raise SystemExit("unsafe Caddy mutation lock")
lock_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
lock_flags |= getattr(os, "O_NOFOLLOW", 0)
mutation_lock_descriptor = os.open(mutation_lock_path, lock_flags)
opened_lock = os.fstat(mutation_lock_descriptor)
if (
    (opened_lock.st_dev, opened_lock.st_ino) != (lock_stat.st_dev, lock_stat.st_ino)
    or not stat.S_ISREG(opened_lock.st_mode)
    or opened_lock.st_uid != 0
    or opened_lock.st_gid != 0
    or stat.S_IMODE(opened_lock.st_mode) != 0o600
    or opened_lock.st_nlink != 1
):
    raise SystemExit("Caddy mutation lock changed while opening")
fcntl.flock(mutation_lock_descriptor, fcntl.LOCK_EX)
current_lock = os.lstat(mutation_lock_path)
if (current_lock.st_dev, current_lock.st_ino) != (
    lock_stat.st_dev,
    lock_stat.st_ino,
):
    raise SystemExit("Caddy mutation lock changed while acquiring")
fence_stat = os.lstat(fence_state_path)
if (
    not stat.S_ISREG(fence_stat.st_mode)
    or fence_stat.st_uid != 0
    or fence_stat.st_gid != 0
    or stat.S_IMODE(fence_stat.st_mode) != 0o600
    or fence_stat.st_nlink != 1
):
    raise SystemExit("unsafe Caddy mutation fence token")
fence_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
fence_flags |= getattr(os, "O_NOFOLLOW", 0)
fence_descriptor = os.open(fence_state_path, fence_flags)
try:
    opened_fence = os.fstat(fence_descriptor)
    if (
        (opened_fence.st_dev, opened_fence.st_ino)
        != (fence_stat.st_dev, fence_stat.st_ino)
        or not stat.S_ISREG(opened_fence.st_mode)
        or opened_fence.st_uid != 0
        or opened_fence.st_gid != 0
        or stat.S_IMODE(opened_fence.st_mode) != 0o600
        or opened_fence.st_nlink != 1
    ):
        raise SystemExit("Caddy mutation fence changed while opening")
    fence_bytes = os.read(fence_descriptor, 66)
finally:
    os.close(fence_descriptor)
if fence_bytes != (fence_token + "\\n").encode("ascii"):
    raise SystemExit("Caddy mutation fence token changed")
"""
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
TRANSACTION_ID_PATTERN = re.compile(r"(?P<transaction_id>\d{8}T\d{6}Z-[0-9a-f]{32})\Z")
BACKUP_NAME_PATTERN = re.compile(
    r"commerce-ops-desk\."
    r"(?P<transaction_id>\d{8}T\d{6}Z-[0-9a-f]{32})\.conf\Z"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
COMPOSE_SERVICE = "commerce-ops-desk"
BUILD_MANIFEST_SCHEMA = 2
CANDIDATE_STATE_SCHEMA = 2
DEPLOYMENT_ASSET_SCHEMA = 1
DEPLOYMENT_ASSET_PATHS = (
    "deploy/workstation/compose.yaml",
    "deploy/workstation/commerce-ops-desk.Caddyfile.template",
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template",
)
COMPOSE_ASSET_PATH = DEPLOYMENT_ASSET_PATHS[0]
CADDY_PROFILE_ASSET_PATHS = {
    CADDY_PROFILE_HARDENED: DEPLOYMENT_ASSET_PATHS[1],
    CADDY_PROFILE_LEGACY_V020: DEPLOYMENT_ASSET_PATHS[2],
}
MAX_DEPLOYMENT_ASSET_BYTES = 1_048_576
RUNTIME_HASH_DOMAIN = b"commerce-ops-desk-runtime-v1\0"
UPSTREAM_HASH_DOMAIN = b"commerce-ops-desk-upstream-v1\0"
ROUTE_REVISION_HASH_DOMAIN = b"commerce-ops-desk-route-v1\0"
UPSTREAM_IDENTITY_SCHEMA = 1
ACTIVE_STATE_SCHEMA = 1
CADDY_TRANSACTION_SCHEMA = 3
LEGACY_UPSTREAM_FINGERPRINT = (
    "b443797953fc44f8acd0e8a353bec2853d4a100fdf113a48d2815754ce4c30ac"
)
REQUIRED_MASKED_PATHS = frozenset(
    {
        "/proc/acpi",
        "/proc/asound",
        "/proc/interrupts",
        "/proc/kcore",
        "/proc/keys",
        "/proc/latency_stats",
        "/proc/sched_debug",
        "/proc/scsi",
        "/proc/timer_list",
        "/proc/timer_stats",
        "/sys/devices/virtual/powercap",
        "/sys/firmware",
    }
)
REQUIRED_READONLY_PATHS = frozenset(
    {
        "/proc/bus",
        "/proc/fs",
        "/proc/irq",
        "/proc/sys",
        "/proc/sysrq-trigger",
    }
)
HOST_CONFIG_ALLOWED_FIELDS = frozenset(
    {
        "Annotations",
        "AutoRemove",
        "Binds",
        "BlkioDeviceReadBps",
        "BlkioDeviceReadIOps",
        "BlkioDeviceWriteBps",
        "BlkioDeviceWriteIOps",
        "BlkioWeight",
        "BlkioWeightDevice",
        "CapAdd",
        "CapDrop",
        "Cgroup",
        "CgroupParent",
        "CgroupnsMode",
        "ConsoleSize",
        "ContainerIDFile",
        "CpuCount",
        "CpuPercent",
        "CpuPeriod",
        "CpuQuota",
        "CpuRealtimePeriod",
        "CpuRealtimeRuntime",
        "CpuShares",
        "CpusetCpus",
        "CpusetMems",
        "DeviceCgroupRules",
        "DeviceRequests",
        "Devices",
        "Dns",
        "DnsOptions",
        "DnsSearch",
        "ExtraHosts",
        "GroupAdd",
        "IOMaximumBandwidth",
        "IOMaximumIOps",
        "Init",
        "IpcMode",
        "Isolation",
        "KernelMemoryTCP",
        "Links",
        "LogConfig",
        "MaskedPaths",
        "Memory",
        "MemoryReservation",
        "MemorySwap",
        "MemorySwappiness",
        "NanoCpus",
        "NetworkMode",
        "OomKillDisable",
        "OomScoreAdj",
        "PidMode",
        "PidsLimit",
        "PortBindings",
        "Privileged",
        "PublishAllPorts",
        "ReadonlyPaths",
        "ReadonlyRootfs",
        "RestartPolicy",
        "Runtime",
        "SecurityOpt",
        "ShmSize",
        "StorageOpt",
        "Sysctls",
        "Tmpfs",
        "UTSMode",
        "Ulimits",
        "UsernsMode",
        "VolumeDriver",
        "VolumesFrom",
    }
)
HOST_CONFIG_REQUIRED_SAFE_FIELDS = frozenset(
    {
        "AutoRemove",
        "Binds",
        "BlkioWeight",
        "CgroupParent",
        "DeviceCgroupRules",
        "Dns",
        "DnsOptions",
        "DnsSearch",
        "ExtraHosts",
        "GroupAdd",
        "Links",
        "MaskedPaths",
        "MemoryReservation",
        "MemorySwap",
        "MemorySwappiness",
        "OomKillDisable",
        "PublishAllPorts",
        "ReadonlyPaths",
        "Runtime",
        "ShmSize",
        "Sysctls",
        "Ulimits",
        "VolumeDriver",
    }
)


class DeploymentError(RuntimeError):
    """A safe, operator-actionable deployment failure."""


class IndeterminateCaddyMutationError(DeploymentError):
    """A privileged Caddy mutation may still have taken effect."""


@dataclass(frozen=True)
class CandidateIdentity:
    source_sha: str
    project_name: str
    container_name: str
    image: str
    data_directory_name: str


DataDirectoryIdentity = tuple[int, int, int, int]


@dataclass(frozen=True)
class CandidateNetworkContract:
    """Validated identity of the one private candidate network."""

    network_id: str
    subnet: str
    gateway: str
    endpoint_id: str | None
    ipv4_address: str | None

    @property
    def trusted_proxy_json(self) -> str:
        return json.dumps([f"{self.gateway}/32"], separators=(",", ":"))


@dataclass(frozen=True)
class ValidatedCaddyFragment:
    """A fragment matching exactly one frozen managed profile."""

    profile: str
    upstream_port: int
    route_revision: str | None = None


@dataclass(frozen=True)
class TrustedCaddyBackup:
    """One rollback fragment authenticated by the root-owned transaction ledger."""

    path: Path
    fragment: str
    upstream_port: int
    profile: str
    parent: ActiveCaddyChain


@dataclass(frozen=True)
class VerifiedDeploymentAssets:
    """Approved deployment-control bytes held in memory for one operation."""

    identity: Mapping[str, object]
    compose_yaml: str
    caddy_templates: Mapping[str, str]


@dataclass(frozen=True)
class UpstreamIdentity:
    schema: int
    docker_daemon_id: str
    container_id: str
    container_name: str
    image_id: str
    image_reference: str
    source_sha: str
    host_port: int
    data_path: str
    data_device: int
    data_inode: int
    network_name: str
    network_id: str
    network_endpoint_id: str
    runtime_sha256: str


@dataclass(frozen=True)
class RouteState:
    fragment_sha256: str
    profile: str
    route_revision: str | None
    upstream: UpstreamIdentity
    deployment_assets: Mapping[str, Any] | None


@dataclass(frozen=True)
class PersistedCaddyTransaction:
    transaction_id: str
    operation: str
    bootstrap_id: str
    backup_path: Path
    ledger_path: Path
    ledger_text: str
    ledger_sha256: str
    backup: RouteState
    installed: RouteState


@dataclass(frozen=True)
class ActiveCaddyChain:
    active_text: str
    active_sha256: str
    bootstrap_id: str
    transaction_id: str
    installed: RouteState
    transaction: PersistedCaddyTransaction


@dataclass(frozen=True)
class VerifiedCandidate:
    deployment_assets: VerifiedDeploymentAssets
    upstream: UpstreamIdentity


def _upstream_identity_payload(identity: UpstreamIdentity) -> dict[str, object]:
    return {
        "schema": identity.schema,
        "docker_daemon_id": identity.docker_daemon_id,
        "container_id": identity.container_id,
        "container_name": identity.container_name,
        "image_id": identity.image_id,
        "image_reference": identity.image_reference,
        "source_sha": identity.source_sha,
        "host_port": identity.host_port,
        "data_path": identity.data_path,
        "data_device": identity.data_device,
        "data_inode": identity.data_inode,
        "network_name": identity.network_name,
        "network_id": identity.network_id,
        "network_endpoint_id": identity.network_endpoint_id,
        "runtime_sha256": identity.runtime_sha256,
    }


def _validate_upstream_identity_payload(value: object) -> UpstreamIdentity:
    expected_fields = {
        "schema",
        "docker_daemon_id",
        "container_id",
        "container_name",
        "image_id",
        "image_reference",
        "source_sha",
        "host_port",
        "data_path",
        "data_device",
        "data_inode",
        "network_name",
        "network_id",
        "network_endpoint_id",
        "runtime_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise DeploymentError("upstream identity schema is invalid")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise DeploymentError("upstream identity schema is invalid")
    docker_daemon_id = _validate_docker_daemon_id(
        value.get("docker_daemon_id"), label="upstream identity"
    )
    container_id = value.get("container_id")
    container_name = value.get("container_name")
    image_id = value.get("image_id")
    image_reference = value.get("image_reference")
    source_sha = value.get("source_sha")
    if (
        not isinstance(container_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        or not isinstance(container_name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container_name) is None
        or not isinstance(image_id, str)
        or IMAGE_ID_PATTERN.fullmatch(image_id) is None
        or not isinstance(image_reference, str)
        or not image_reference
        or any(character in image_reference for character in "\r\n\0")
        or not isinstance(source_sha, str)
        or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
    ):
        raise DeploymentError("upstream identity container or image fields are invalid")
    host_port = value.get("host_port")
    if type(host_port) is not int:
        raise DeploymentError("upstream identity host port is invalid")
    try:
        validated_port = validate_candidate_port(str(host_port))
    except DeploymentError:
        raise DeploymentError("upstream identity host port is invalid") from None
    data_path = value.get("data_path")
    data_device = value.get("data_device")
    data_inode = value.get("data_inode")
    if (
        not isinstance(data_path, str)
        or not Path(data_path).is_absolute()
        or str(Path(data_path)) != data_path
        or type(data_device) is not int
        or data_device < 0
        or type(data_inode) is not int
        or data_inode <= 0
    ):
        raise DeploymentError("upstream identity data fields are invalid")
    network_name = value.get("network_name")
    network_id = value.get("network_id")
    network_endpoint_id = value.get("network_endpoint_id")
    runtime_sha256 = value.get("runtime_sha256")
    if (
        not isinstance(network_name, str)
        or not network_name
        or any(character in network_name for character in "\r\n\0")
        or not isinstance(network_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(network_id) is None
        or not isinstance(network_endpoint_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(network_endpoint_id) is None
        or not isinstance(runtime_sha256, str)
        or SHA256_PATTERN.fullmatch(runtime_sha256) is None
    ):
        raise DeploymentError("upstream identity network or runtime fields are invalid")
    return UpstreamIdentity(
        schema=UPSTREAM_IDENTITY_SCHEMA,
        docker_daemon_id=docker_daemon_id,
        container_id=container_id,
        container_name=container_name,
        image_id=image_id,
        image_reference=image_reference,
        source_sha=source_sha,
        host_port=validated_port,
        data_path=data_path,
        data_device=data_device,
        data_inode=data_inode,
        network_name=network_name,
        network_id=network_id,
        network_endpoint_id=network_endpoint_id,
        runtime_sha256=runtime_sha256,
    )


def _canonical_identity_sha256(domain: bytes, payload: object, *, label: str) -> str:
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError):
        raise DeploymentError(f"{label} is not canonical JSON") from None
    return hashlib.sha256(domain + canonical).hexdigest()


def _stable_json_record(payload: object, *, label: str) -> str:
    try:
        return (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )
    except (TypeError, UnicodeEncodeError):
        raise DeploymentError(f"{label} is not canonical JSON") from None


def _upstream_identity_fingerprint(identity: UpstreamIdentity) -> str:
    validated = _validate_upstream_identity_payload(
        _upstream_identity_payload(identity)
    )
    return _canonical_identity_sha256(
        UPSTREAM_HASH_DOMAIN,
        _upstream_identity_payload(validated),
        label="upstream identity",
    )


def _route_revision(
    profile: str,
    upstream: UpstreamIdentity,
    deployment_assets: object,
) -> str:
    if profile not in CADDY_MANAGED_PROFILES:
        raise DeploymentError("route revision profile is invalid")
    validated_upstream = _validate_upstream_identity_payload(
        _upstream_identity_payload(upstream)
    )
    validated_assets = _validate_deployment_assets(deployment_assets)
    return _canonical_identity_sha256(
        ROUTE_REVISION_HASH_DOMAIN,
        {
            "profile": profile,
            "upstream": _upstream_identity_payload(validated_upstream),
            "deployment_assets": validated_assets,
        },
        label="route revision",
    )


def _route_state_payload(route: RouteState) -> dict[str, object]:
    return {
        "fragment_sha256": route.fragment_sha256,
        "profile": route.profile,
        "route_revision": route.route_revision,
        "upstream": _upstream_identity_payload(route.upstream),
        "deployment_assets": (
            None if route.deployment_assets is None else dict(route.deployment_assets)
        ),
    }


def _validate_route_state_payload(value: object) -> RouteState:
    expected_fields = {
        "fragment_sha256",
        "profile",
        "route_revision",
        "upstream",
        "deployment_assets",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise DeploymentError("route state schema is invalid")
    fragment_sha256 = value.get("fragment_sha256")
    profile = value.get("profile")
    if (
        not isinstance(fragment_sha256, str)
        or SHA256_PATTERN.fullmatch(fragment_sha256) is None
        or not isinstance(profile, str)
        or profile not in CADDY_MANAGED_PROFILES
    ):
        raise DeploymentError("route state identity is invalid")
    upstream = _validate_upstream_identity_payload(value.get("upstream"))
    revision = value.get("route_revision")
    raw_assets = value.get("deployment_assets")
    if (revision is None) != (raw_assets is None):
        raise DeploymentError(
            "route revision and deployment assets must be present as a pair"
        )
    if revision is None:
        if profile != CADDY_PROFILE_LEGACY_V020:
            raise DeploymentError("route revision is required for hardened routes")
        assets: Mapping[str, Any] | None = None
    else:
        if (
            profile != CADDY_PROFILE_HARDENED
            or not isinstance(revision, str)
            or SHA256_PATTERN.fullmatch(revision) is None
        ):
            raise DeploymentError("route revision is invalid")
        validated_assets = _validate_deployment_assets(raw_assets)
        if revision != _route_revision(profile, upstream, validated_assets):
            raise DeploymentError("route revision is not bound to its route state")
        assets = MappingProxyType(validated_assets)
    return RouteState(
        fragment_sha256=fragment_sha256,
        profile=profile,
        route_revision=revision,
        upstream=upstream,
        deployment_assets=assets,
    )


def _validate_site_identity(value: object, *, label: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "host"}
        or value.get("path") != str(CADDY_SITE)
        or value.get("host") != CADDY_HOST
    ):
        raise DeploymentError(f"{label} site identity is invalid")


def _validate_active_state_payload(value: object) -> dict[str, Any]:
    expected_fields = {
        "schema",
        "bootstrap_id",
        "transaction_id",
        "ledger_path",
        "ledger_sha256",
        "site",
        "installed",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise DeploymentError("active Caddy state schema is invalid")
    bootstrap_id = value.get("bootstrap_id")
    transaction_id = value.get("transaction_id")
    ledger_path = value.get("ledger_path")
    ledger_sha256 = value.get("ledger_sha256")
    if (
        type(value.get("schema")) is not int
        or value["schema"] != ACTIVE_STATE_SCHEMA
        or not isinstance(bootstrap_id, str)
        or SHA256_PATTERN.fullmatch(bootstrap_id) is None
        or not isinstance(transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
        or not isinstance(ledger_path, str)
        or not Path(ledger_path).is_absolute()
        or not isinstance(ledger_sha256, str)
        or SHA256_PATTERN.fullmatch(ledger_sha256) is None
    ):
        raise DeploymentError("active Caddy state identity is invalid")
    _validate_site_identity(value.get("site"), label="active Caddy state")
    _validate_route_state_payload(value.get("installed"))
    return cast(dict[str, Any], value)


def _validate_transaction_ledger_payload(value: object) -> dict[str, Any]:
    expected_fields = {
        "schema",
        "transaction_id",
        "operation",
        "bootstrap_id",
        "parent",
        "site",
        "backup",
        "installed",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise DeploymentError("Caddy transaction ledger schema is invalid")
    transaction_id = value.get("transaction_id")
    bootstrap_id = value.get("bootstrap_id")
    if (
        type(value.get("schema")) is not int
        or value["schema"] != CADDY_TRANSACTION_SCHEMA
        or not isinstance(transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
        or value.get("operation") not in {"switch", "rollback"}
        or not isinstance(bootstrap_id, str)
        or SHA256_PATTERN.fullmatch(bootstrap_id) is None
    ):
        raise DeploymentError("Caddy transaction ledger identity is invalid")
    parent = value.get("parent")
    if not isinstance(parent, dict) or set(parent) != {
        "transaction_id",
        "active_sha256",
    }:
        raise DeploymentError("Caddy transaction parent head is invalid")
    parent_transaction_id = parent.get("transaction_id")
    parent_active_sha256 = parent.get("active_sha256")
    if (parent_transaction_id is None) != (parent_active_sha256 is None):
        raise DeploymentError("Caddy transaction parent head is partial")
    if parent_transaction_id is not None and (
        not isinstance(parent_transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(parent_transaction_id) is None
        or not isinstance(parent_active_sha256, str)
        or SHA256_PATTERN.fullmatch(parent_active_sha256) is None
    ):
        raise DeploymentError("Caddy transaction parent head is invalid")
    if value.get("operation") == "rollback" and parent_transaction_id is None:
        raise DeploymentError("Caddy rollback transaction requires a parent head")
    if parent_transaction_id == transaction_id:
        raise DeploymentError("Caddy transaction cannot be its own parent")
    _validate_site_identity(value.get("site"), label="Caddy transaction")
    backup = value.get("backup")
    route_fields = {
        "fragment_sha256",
        "profile",
        "route_revision",
        "upstream",
        "deployment_assets",
    }
    if not isinstance(backup, dict) or set(backup) != {"path", *route_fields}:
        raise DeploymentError("Caddy transaction backup state is invalid")
    backup_path = backup.get("path")
    if not isinstance(backup_path, str) or not Path(backup_path).is_absolute():
        raise DeploymentError("Caddy transaction backup path is invalid")
    _validate_route_state_payload({field: backup[field] for field in route_fields})
    installed = _validate_route_state_payload(value.get("installed"))
    if (
        value.get("operation") == "switch"
        and installed.profile != CADDY_PROFILE_HARDENED
    ):
        raise DeploymentError("Caddy switch must install the hardened profile")
    return cast(dict[str, Any], value)


def _local_docker_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {
        name: value
        for name, value in inherited.items()
        if not name.startswith(("DOCKER_", "COMPOSE_"))
    }
    environment.update(
        {
            "DOCKER_CONFIG": "/etc/docker",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "PATH": SAFE_SYSTEM_PATH,
        }
    )
    return environment


class CommandRunner:
    """Run fixed argv commands without a shell or inherited secret rendering."""

    _caddy_mutation_fence_token: str

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
        timeout: float | None = COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        if command and command[0] == SUDO_BINARY:
            if len(command) < 2:
                raise DeploymentError("sudo requires an allowlisted privileged command")
            if not os.path.isabs(command[1]):
                resolved_subcommand = SAFE_SUDO_SUBCOMMANDS.get(command[1])
                if resolved_subcommand is None:
                    raise DeploymentError(
                        "sudo privileged subcommand is not allowlisted"
                    )
                command[1] = resolved_subcommand
            environment = SAFE_PRIVILEGED_ENVIRONMENT
        elif command and command[0] == DOCKER_BINARY:
            environment = _local_docker_environment(environment)
        try:
            result = subprocess.run(
                command,
                env=None if environment is None else dict(environment),
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeploymentError(
                f"command could not complete: {command[0]}"
            ) from error
        if result.returncode not in allowed_returncodes:
            detail = result.stderr.strip() or result.stdout.strip()
            if len(detail) > 2_000:
                detail = detail[:2_000] + "..."
            suffix = f": {detail}" if detail else ""
            raise DeploymentError(
                f"command failed ({command[0]}, exit {result.returncode}){suffix}"
            )
        return result


def _exception_contains_timeout(error: BaseException) -> bool:
    pending: BaseException | None = error
    seen: set[int] = set()
    while pending is not None and id(pending) not in seen:
        seen.add(id(pending))
        if isinstance(pending, subprocess.TimeoutExpired):
            return True
        pending = pending.__cause__ or pending.__context__
    return False


def validate_source_sha(value: str) -> str:
    if SOURCE_SHA_PATTERN.fullmatch(value) is None:
        raise DeploymentError(
            "SOURCE_SHA must be one full 40-character lowercase Git SHA"
        )
    return value


def candidate_identity(source_sha: str) -> CandidateIdentity:
    validated = validate_source_sha(source_sha)
    short_sha = validated[:12]
    return CandidateIdentity(
        source_sha=validated,
        project_name=f"commerce-ops-candidate-{short_sha}",
        container_name=f"app-commerce-ops-desk-candidate-{short_sha}",
        image=f"commerce-ops-desk:{validated}",
        data_directory_name=f"data-candidate-{short_sha}",
    )


def validate_candidate_port(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise DeploymentError("candidate port must be a canonical decimal integer")
    port = int(value)
    if str(port) != value or not 1_024 <= port <= 65_535:
        raise DeploymentError(
            "candidate port must be a canonical integer from 1024 to 65535"
        )
    return port


def _validate_directory(
    path: Path,
    *,
    label: str,
    app_root: Path,
    expected_uid: int,
    expected_gid: int,
) -> Path:
    if not path.is_absolute():
        raise DeploymentError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise DeploymentError(f"{label} must not be a symbolic link")
    try:
        resolved_root = app_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise DeploymentError(f"{label} must already exist") from None
    if resolved != path:
        raise DeploymentError(
            f"{label} must not contain a symbolic link or non-canonical path"
        )
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise DeploymentError(f"{label} must be inside {resolved_root}")

    metadata = resolved.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise DeploymentError(f"{label} must be a directory")
    if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        raise DeploymentError(f"{label} must be owned by {expected_uid}:{expected_gid}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DeploymentError(f"{label} must have mode 0700")
    return resolved


def _data_directory_identity(path: Path) -> DataDirectoryIdentity:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise DeploymentError(
            "data directory identity could not be inspected"
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev < 0
        or metadata.st_ino <= 0
        or metadata.st_uid < 0
        or metadata.st_gid < 0
    ):
        raise DeploymentError("data directory identity is invalid")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
    )


def _assert_data_directory_identity(
    path: Path,
    expected: DataDirectoryIdentity,
    *,
    label: str,
) -> None:
    if (
        not isinstance(expected, tuple)
        or len(expected) != 4
        or any(type(value) is not int or value < 0 for value in expected)
        or expected[1] == 0
    ):
        raise DeploymentError(f"recorded {label} identity is invalid")
    if _data_directory_identity(path) != expected:
        raise DeploymentError(f"{label} identity changed after preparation")


def _assert_container_data_identity(
    raw_identity: str,
    expected: DataDirectoryIdentity,
) -> None:
    fields = raw_identity.strip().split("|")
    if len(fields) != 4 or any(
        not field or not field.isascii() or not field.isdigit() for field in fields
    ):
        raise DeploymentError("container data directory identity is invalid")
    values = tuple(int(field) for field in fields)
    if (
        any(str(value) != field for value, field in zip(values, fields, strict=True))
        or values[1] == 0
        or values != expected
    ):
        raise DeploymentError("container data directory identity does not match host")


def _assert_container_data_mount_identity(
    runner: CommandRunner,
    container_id: str,
    expected: DataDirectoryIdentity,
) -> None:
    if CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
        raise DeploymentError("candidate container ID is invalid")
    result = runner.run(
        [
            DOCKER_BINARY,
            "exec",
            "--user",
            f"{RUNTIME_UID}:{RUNTIME_GID}",
            container_id,
            "/usr/bin/stat",
            "--dereference",
            "--format=%d|%i|%u|%g",
            "--",
            "/app/data",
        ]
    )
    _assert_container_data_identity(result.stdout, expected)


def validate_data_directories(
    *,
    app_root: Path,
    live_data_dir: Path,
    candidate_data_dir: Path,
    source_sha: str,
    expected_uid: int = RUNTIME_UID,
    expected_gid: int = RUNTIME_GID,
) -> tuple[Path, Path]:
    identity = candidate_identity(source_sha)
    live = _validate_directory(
        live_data_dir,
        label="live data directory",
        app_root=app_root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    candidate = _validate_directory(
        candidate_data_dir,
        label="candidate data directory",
        app_root=app_root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    expected_candidate = app_root.resolve(strict=True) / identity.data_directory_name
    if candidate != expected_candidate:
        raise DeploymentError(
            f"candidate data directory must be exactly {expected_candidate}"
        )
    if live == candidate:
        raise DeploymentError(
            "candidate data directory must be independent from live data"
        )
    if _data_directory_identity(live)[:2] == _data_directory_identity(candidate)[:2]:
        raise DeploymentError(
            "live and candidate paths resolve to the same directory identity"
        )
    return live, candidate


def validate_deployment_layout(
    *,
    product_root: Path,
    app_root: Path,
    state_directory: Path,
    live_data_dir: Path,
    candidate_data_dir: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    paths: dict[str, Path] = {}
    for label, path in (
        ("application root", app_root),
        ("code root", product_root),
        ("state directory", state_directory),
        ("live data directory", live_data_dir),
        ("candidate data directory", candidate_data_dir),
    ):
        if not path.is_absolute():
            raise DeploymentError(f"{label} must be an absolute path")
        try:
            paths[label] = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            raise DeploymentError(f"{label} must already exist") from None
        if paths[label] != path:
            raise DeploymentError(f"{label} must be canonical and contain no symlink")

    resolved_app = paths["application root"]
    resolved_code = paths["code root"]
    resolved_state = paths["state directory"]
    resolved_live = paths["live data directory"]
    resolved_candidate = paths["candidate data directory"]
    if resolved_code != resolved_app / "code":
        raise DeploymentError("code root must be exactly APP_ROOT/code")
    if resolved_state != resolved_app / "deploy-state":
        raise DeploymentError("state directory must be exactly APP_ROOT/deploy-state")
    for label, data_path in (
        ("live data directory", resolved_live),
        ("candidate data directory", resolved_candidate),
    ):
        if data_path == resolved_code or data_path.is_relative_to(resolved_code):
            raise DeploymentError(f"{label} must be outside the code repository")
        if data_path == resolved_state or data_path.is_relative_to(resolved_state):
            raise DeploymentError(f"{label} must be outside the state directory")
        if resolved_state.is_relative_to(data_path):
            raise DeploymentError(f"state directory must be outside {label}")
    if resolved_live.is_relative_to(
        resolved_candidate
    ) or resolved_candidate.is_relative_to(resolved_live):
        raise DeploymentError(
            "live and candidate data directories must not contain one another"
        )
    return (
        resolved_code,
        resolved_app,
        resolved_state,
        resolved_live,
        resolved_candidate,
    )


def trusted_proxy_json_from_network_inspection(inspection: object) -> str:
    gateways: list[IPv4Address] = []
    if isinstance(inspection, list) and len(inspection) == 1:
        network = inspection[0]
        if isinstance(network, dict):
            ipam = network.get("IPAM")
            if isinstance(ipam, dict):
                configurations = ipam.get("Config")
                if isinstance(configurations, list):
                    for configuration in configurations:
                        if not isinstance(configuration, dict):
                            continue
                        raw_gateway = configuration.get("Gateway")
                        raw_subnet = configuration.get("Subnet")
                        if not isinstance(raw_gateway, str):
                            continue
                        try:
                            address = ip_address(raw_gateway)
                            if not isinstance(address, IPv4Address):
                                continue
                            if raw_subnet is not None and address not in ip_network(
                                cast(str, raw_subnet), strict=False
                            ):
                                continue
                        except (TypeError, ValueError):
                            continue
                        gateways.append(address)
    if len(gateways) != 1:
        raise DeploymentError("candidate network must expose exactly one IPv4 gateway")
    return json.dumps([f"{gateways[0].compressed}/32"], separators=(",", ":"))


def assert_port_unclaimed(
    port: int,
    *,
    listening_ports: set[int],
    docker_inspections: Sequence[object],
    caddy_documents: Mapping[str, str],
) -> None:
    if port in listening_ports:
        raise DeploymentError(f"candidate port {port} already has a listener")

    owners: list[str] = []
    for raw_inspection in docker_inspections:
        if not isinstance(raw_inspection, dict):
            continue
        name = str(raw_inspection.get("Name", "unknown")).lstrip("/")
        host_config = raw_inspection.get("HostConfig")
        if not isinstance(host_config, dict):
            continue
        bindings = host_config.get("PortBindings")
        if not isinstance(bindings, dict):
            continue
        for raw_bindings in bindings.values():
            if not isinstance(raw_bindings, list):
                continue
            for binding in raw_bindings:
                if isinstance(binding, dict) and binding.get("HostPort") == str(port):
                    owners.append(name)
    if owners:
        raise DeploymentError(
            f"candidate port {port} is assigned to Docker container(s): "
            + ", ".join(sorted(set(owners)))
        )

    port_pattern = re.compile(rf"127\.0\.0\.1:{port}(?![0-9])")
    references = [
        name
        for name, document in caddy_documents.items()
        if port_pattern.search(document)
    ]
    if references:
        raise DeploymentError(
            f"candidate port {port} is referenced by Caddy: "
            + ", ".join(sorted(references))
        )


def render_caddy_template(
    template: str,
    port: int,
    *,
    route_revision: str | None = None,
) -> str:
    if template.count(CADDY_PORT_PLACEHOLDER) != 1:
        raise DeploymentError(
            "Caddy template must contain exactly one upstream placeholder"
        )
    revision_placeholders = template.count(CADDY_REVISION_PLACEHOLDER)
    if revision_placeholders == 0:
        if route_revision is not None:
            raise DeploymentError(
                "legacy Caddy template must not receive a route revision"
            )
        rendered_revision = template
    elif revision_placeholders == 1:
        if (
            not isinstance(route_revision, str)
            or SHA256_PATTERN.fullmatch(route_revision) is None
        ):
            raise DeploymentError(
                "hardened Caddy template requires one canonical route revision"
            )
        rendered_revision = template.replace(
            CADDY_REVISION_PLACEHOLDER,
            route_revision,
        )
    else:
        raise DeploymentError(
            "Caddy template must contain at most one route revision placeholder"
        )
    return rendered_revision.replace(CADDY_PORT_PLACEHOLDER, str(port))


def _parse_json(raw: str, *, label: str) -> object:
    try:
        return cast(object, json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        raise DeploymentError(f"{label} did not return valid JSON") from None


def _local_docker_daemon_id(runner: CommandRunner) -> str:
    identifier = _parse_json(
        runner.run([DOCKER_BINARY, "info", "--format", "{{json .ID}}"]).stdout,
        label="local Docker daemon identity",
    )
    return _validate_docker_daemon_id(identifier, label="local Docker daemon")


def _assert_local_docker_daemon(
    runner: CommandRunner,
    expected_daemon_id: str,
) -> None:
    validated_expected = _validate_docker_daemon_id(
        expected_daemon_id,
        label="expected Docker daemon",
    )
    if _local_docker_daemon_id(runner) != validated_expected:
        raise DeploymentError(
            "local Docker daemon does not match the verified deployment identity"
        )


def _listening_ports(runner: CommandRunner) -> set[int]:
    result = runner.run([SS_BINARY, "-H", "-ltn"])
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            continue
        match = re.search(r":([0-9]+)\Z", columns[3])
        if match is not None:
            ports.add(int(match.group(1)))
    return ports


def _docker_port_inspections(runner: CommandRunner) -> list[object]:
    identifiers = runner.run(
        [DOCKER_BINARY, "container", "ls", "--all", "--quiet", "--no-trunc"]
    ).stdout.split()
    inspections: list[object] = []
    for identifier in identifiers:
        result = runner.run(
            [
                DOCKER_BINARY,
                "inspect",
                "--format",
                "{{json .Name}}\t{{json .HostConfig.PortBindings}}",
                identifier,
            ]
        )
        name_raw, separator, bindings_raw = result.stdout.strip().partition("\t")
        if not separator:
            raise DeploymentError("Docker port inspection returned an invalid shape")
        inspections.append(
            {
                "Name": _parse_json(name_raw, label="Docker container name"),
                "HostConfig": {
                    "PortBindings": _parse_json(
                        bindings_raw,
                        label="Docker port bindings",
                    )
                },
            }
        )
    return inspections


def _caddy_port_documents(runner: CommandRunner, port: int) -> dict[str, str]:
    target = f"127.0.0.1:{port}"
    disk_document = _parse_json(
        runner.run(
            [
                SUDO_BINARY,
                CADDY_BINARY,
                "adapt",
                "--adapter",
                "caddyfile",
                "--config",
                str(CADDY_MAIN_CONFIG),
            ]
        ).stdout,
        label="on-disk Caddy configuration",
    )
    if not isinstance(disk_document, dict):
        raise DeploymentError("on-disk Caddy configuration is not a document")
    active_document = _active_caddy_config(runner)
    references: dict[str, str] = {}
    if port in _caddy_loopback_ports(disk_document):
        references["on-disk Caddy configuration"] = target
    if port in _caddy_loopback_ports(active_document):
        references["active Caddy configuration"] = target
    return references


def _caddy_loopback_ports(document: object) -> set[int]:
    ports: set[int] = set()

    def parse_dial(raw_dial: object) -> None:
        if not isinstance(raw_dial, str):
            raise DeploymentError("Caddy reverse-proxy upstream is not static")
        bracketed = re.fullmatch(r"\[([^\]]+)\]:([0-9]+)", raw_dial)
        unbracketed = re.fullmatch(r"([^:]+):([0-9]+)", raw_dial)
        match = bracketed or unbracketed
        if match is None:
            raise DeploymentError("Caddy reverse-proxy upstream is not static")
        raw_host, raw_port = match.groups()
        try:
            parsed_port = int(raw_port)
        except ValueError:  # pragma: no cover - constrained by the regular expression.
            raise DeploymentError("Caddy reverse-proxy port is invalid") from None
        if not 1 <= parsed_port <= 65_535:
            raise DeploymentError("Caddy reverse-proxy port is invalid")
        if raw_host.lower() == "localhost":
            ports.add(parsed_port)
            return
        try:
            address = ip_address(raw_host)
        except ValueError:
            return
        if address.is_loopback:
            ports.add(parsed_port)

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("handler") == "reverse_proxy":
            upstreams = value.get("upstreams")
            if "dynamic_upstreams" in value or not isinstance(upstreams, list):
                raise DeploymentError(
                    "Caddy reverse-proxy upstream contract is dynamic or invalid"
                )
            for upstream in upstreams:
                if not isinstance(upstream, dict) or "dial" not in upstream:
                    raise DeploymentError(
                        "Caddy reverse-proxy upstream contract is invalid"
                    )
                parse_dial(upstream["dial"])
        for child in value.values():
            visit(child)

    visit(document)
    return ports


def _assert_port_available(runner: CommandRunner, port: int) -> None:
    assert_port_unclaimed(
        port,
        listening_ports=_listening_ports(runner),
        docker_inspections=_docker_port_inspections(runner),
        caddy_documents=_caddy_port_documents(runner, port),
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise DeploymentError(
                f"candidate port {port} cannot be bound on loopback"
            ) from None


def _assert_image_revision(
    runner: CommandRunner,
    identity: CandidateIdentity,
    *,
    expected_image_id: str | None = None,
) -> str:
    inspection = _parse_json(
        runner.run([DOCKER_BINARY, "image", "inspect", identity.image]).stdout,
        label="candidate image inspection",
    )
    if (
        not isinstance(inspection, list)
        or len(inspection) != 1
        or not isinstance(inspection[0], dict)
    ):
        raise DeploymentError("candidate image inspection returned an invalid shape")
    image = inspection[0]
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    revision = (
        labels.get("org.opencontainers.image.revision")
        if isinstance(labels, dict)
        else None
    )
    if revision != identity.source_sha:
        raise DeploymentError("candidate image OCI revision does not match SOURCE_SHA")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise DeploymentError("candidate image did not return one immutable image ID")
    if expected_image_id is not None and image_id != expected_image_id:
        raise DeploymentError(
            "candidate image ID does not match the verified build manifest"
        )
    return image_id


def _immutable_image_config(
    runner: CommandRunner,
    identity: CandidateIdentity,
    image_id: str,
) -> dict[str, Any]:
    inspection = _parse_json(
        runner.run([DOCKER_BINARY, "image", "inspect", image_id]).stdout,
        label="immutable candidate image inspection",
    )
    if (
        not isinstance(inspection, list)
        or len(inspection) != 1
        or not isinstance(inspection[0], dict)
        or inspection[0].get("Id") != image_id
    ):
        raise DeploymentError(
            "immutable candidate image inspection returned the wrong image"
        )
    config = inspection[0].get("Config")
    if not isinstance(config, dict):
        raise DeploymentError("immutable candidate image configuration is incomplete")
    labels = config.get("Labels")
    if (
        not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != identity.source_sha
    ):
        raise DeploymentError(
            "immutable candidate image OCI revision does not match SOURCE_SHA"
        )
    environment = config.get("Env")
    runtime_source_shas = (
        [
            entry.partition("=")[2]
            for entry in environment
            if isinstance(entry, str) and entry.startswith("COMMERCE_OPS_SOURCE_SHA=")
        ]
        if isinstance(environment, list)
        else []
    )
    if len(runtime_source_shas) != 1:
        raise DeploymentError(
            "immutable candidate image must define exactly one runtime source SHA"
        )
    if runtime_source_shas[0] != identity.source_sha:
        raise DeploymentError(
            "immutable candidate image runtime source SHA does not match SOURCE_SHA"
        )
    return cast(dict[str, Any], config)


def _assert_candidate_absent(
    runner: CommandRunner, identity: CandidateIdentity
) -> None:
    container = runner.run(
        [DOCKER_BINARY, "container", "inspect", identity.container_name],
        allowed_returncodes=frozenset({0, 1}),
    )
    if container.returncode == 0:
        raise DeploymentError(
            f"candidate container already exists: {identity.container_name}; "
            "follow the runbook cleanup procedure before retrying"
        )
    network = runner.run(
        [
            DOCKER_BINARY,
            "network",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={identity.project_name}",
            "--quiet",
        ]
    )
    if network.stdout.strip():
        raise DeploymentError(
            f"candidate Compose project already exists: {identity.project_name}"
        )


def _compose_environment(
    identity: CandidateIdentity,
    *,
    port: int,
    data_directory: Path,
    trusted_proxy_json: str,
) -> dict[str, str]:
    environment = _local_docker_environment()
    for inherited_name in (
        "COMPOSE_PROJECT_NAME",
        "CONTAINER_NAME",
        "DATA_DIR",
        "HOST_PORT",
        "SOURCE_SHA",
        "TRUSTED_PROXY_CIDRS_JSON",
    ):
        environment.pop(inherited_name, None)
    environment.update(
        {
            "CONTAINER_NAME": identity.container_name,
            "DATA_DIR": str(data_directory),
            "HOST_PORT": str(port),
            "SOURCE_SHA": identity.source_sha,
            "TRUSTED_PROXY_CIDRS_JSON": trusted_proxy_json,
        }
    )
    return environment


def _compose_command(identity: CandidateIdentity, *arguments: str) -> list[str]:
    return [
        DOCKER_BINARY,
        "compose",
        "--project-name",
        identity.project_name,
        "--file",
        "-",
        *arguments,
    ]


def _assert_compose_service_contract(
    runner: CommandRunner,
    identity: CandidateIdentity,
    *,
    compose_yaml: str,
    environment: Mapping[str, str],
) -> None:
    result = runner.run(
        _compose_command(identity, "config", "--services"),
        environment=environment,
        input_text=compose_yaml,
    )
    services = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if services != [COMPOSE_SERVICE]:
        raise DeploymentError(
            "candidate Compose config must contain exactly one managed service"
        )


def _assert_exact_project_container(
    runner: CommandRunner,
    identity: CandidateIdentity,
) -> str:
    result = runner.run(
        [
            DOCKER_BINARY,
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={identity.project_name}",
            "--format",
            "{{.ID}}",
        ]
    )
    identifiers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(identifiers) != 1:
        raise DeploymentError(
            "candidate Compose project must contain exactly one container"
        )
    identifier = identifiers[0]
    if CONTAINER_ID_PATTERN.fullmatch(identifier) is None:
        raise DeploymentError("candidate container ID is invalid")
    inspection = _parse_json(
        runner.run([DOCKER_BINARY, "container", "inspect", identifier]).stdout,
        label="candidate project container inspection",
    )
    if (
        not isinstance(inspection, list)
        or len(inspection) != 1
        or not isinstance(inspection[0], dict)
    ):
        raise DeploymentError("candidate project container identity is invalid")
    container = inspection[0]
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        container.get("Id") != identifier
        or container.get("Name") != f"/{identity.container_name}"
        or not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != identity.project_name
        or labels.get("com.docker.compose.service") != COMPOSE_SERVICE
    ):
        raise DeploymentError("candidate project container identity is invalid")
    return identifier


def _created_project_network(
    runner: CommandRunner,
    identity: CandidateIdentity,
) -> tuple[str, object]:
    result = runner.run(
        [
            DOCKER_BINARY,
            "network",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={identity.project_name}",
            "--format",
            "{{.Name}}",
        ]
    )
    network_names = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    if len(network_names) != 1:
        raise DeploymentError(
            "candidate Compose project must create exactly one network"
        )
    network_name = network_names[0]
    expected_network_name = f"{identity.project_name}_default"
    if network_name != expected_network_name:
        raise DeploymentError(
            f"candidate Compose network must be named {expected_network_name}"
        )
    inspection = _parse_json(
        runner.run([DOCKER_BINARY, "network", "inspect", network_name]).stdout,
        label="Docker network inspection",
    )
    _assert_candidate_network(
        identity,
        network_name,
        inspection,
        expected_container_id=None,
    )
    return network_name, inspection


def _assert_candidate_network(
    identity: CandidateIdentity,
    network_name: str,
    inspection: object,
    *,
    expected_container_id: str | None,
) -> str:
    return _validate_candidate_network_contract(
        identity,
        network_name,
        inspection,
        expected_container_id=expected_container_id,
    ).trusted_proxy_json


def _validate_candidate_network_contract(
    identity: CandidateIdentity,
    network_name: str,
    inspection: object,
    *,
    expected_container_id: str | None,
) -> CandidateNetworkContract:
    if not isinstance(inspection, list) or len(inspection) != 1:
        raise DeploymentError("candidate network inspection returned an invalid shape")
    network = inspection[0]
    if not isinstance(network, dict) or network.get("Name") != network_name:
        raise DeploymentError("candidate network inspection returned the wrong network")
    expected_fields = {
        "Name",
        "Id",
        "Created",
        "Scope",
        "Driver",
        "EnableIPv4",
        "EnableIPv6",
        "IPAM",
        "Internal",
        "Attachable",
        "Ingress",
        "ConfigFrom",
        "ConfigOnly",
        "Containers",
        "Options",
        "Labels",
    }
    if set(network) != expected_fields:
        raise DeploymentError("candidate network contract field set is invalid")
    network_id = network.get("Id")
    if (
        not isinstance(network_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(network_id) is None
        or not isinstance(network.get("Created"), str)
        or not network["Created"]
        or network.get("Scope") != "local"
        or network.get("Driver") != "bridge"
        or network.get("EnableIPv4") is not True
        or network.get("EnableIPv6") is not False
        or network.get("Internal") is not False
        or network.get("Attachable") is not False
        or network.get("Ingress") is not False
        or network.get("ConfigFrom") != {"Network": ""}
        or network.get("ConfigOnly") is not False
        or network.get("Options") != {}
    ):
        raise DeploymentError("candidate network contract is not the private bridge")

    ipam = network.get("IPAM")
    if not isinstance(ipam, dict) or set(ipam) != {"Driver", "Options", "Config"}:
        raise DeploymentError("candidate network contract IPAM shape is invalid")
    configurations = ipam.get("Config")
    if (
        ipam.get("Driver") != "default"
        or ipam.get("Options") is not None
        or not isinstance(configurations, list)
        or len(configurations) != 1
        or not isinstance(configurations[0], dict)
        or set(configurations[0]) != {"Subnet", "Gateway"}
    ):
        raise DeploymentError("candidate network contract IPAM is invalid")
    raw_subnet = configurations[0].get("Subnet")
    raw_gateway = configurations[0].get("Gateway")
    if not isinstance(raw_subnet, str) or not isinstance(raw_gateway, str):
        raise DeploymentError("candidate network contract IPAM is invalid")
    try:
        subnet = ip_network(raw_subnet, strict=True)
        gateway = ip_address(raw_gateway)
    except (TypeError, ValueError):
        raise DeploymentError("candidate network contract IPAM is invalid") from None
    if (
        not isinstance(subnet, IPv4Network)
        or not isinstance(gateway, IPv4Address)
        or gateway not in subnet
        or gateway in {subnet.network_address, subnet.broadcast_address}
    ):
        raise DeploymentError("candidate network contract IPAM is invalid")

    labels = network.get("Labels")
    expected_label_fields = {
        "com.docker.compose.config-hash",
        "com.docker.compose.network",
        "com.docker.compose.project",
        "com.docker.compose.version",
    }
    if (
        not isinstance(labels, dict)
        or set(labels) != expected_label_fields
        or labels.get("com.docker.compose.network") != "default"
        or labels.get("com.docker.compose.project") != identity.project_name
        or labels.get("com.docker.compose.version") != "2.40.3"
        or not isinstance(labels.get("com.docker.compose.config-hash"), str)
        or SHA256_PATTERN.fullmatch(labels["com.docker.compose.config-hash"]) is None
    ):
        raise DeploymentError("candidate network has invalid Compose labels")

    containers = network.get("Containers")
    if not isinstance(containers, dict):
        raise DeploymentError("candidate network endpoint set is invalid")
    endpoint_id: str | None = None
    ipv4_address: str | None = None
    if expected_container_id is None:
        if containers:
            raise DeploymentError("candidate network endpoint set is invalid")
    else:
        endpoint = containers.get(expected_container_id)
        if (
            CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None
            or set(containers) != {expected_container_id}
            or not isinstance(endpoint, dict)
            or endpoint.get("Name") != identity.container_name
        ):
            raise DeploymentError("candidate network endpoint set is invalid")
        expected_endpoint_fields = {
            "Name",
            "EndpointID",
            "MacAddress",
            "IPv4Address",
            "IPv6Address",
        }
        endpoint_id = endpoint.get("EndpointID")
        raw_mac = endpoint.get("MacAddress")
        raw_ipv4 = endpoint.get("IPv4Address")
        if (
            set(endpoint) != expected_endpoint_fields
            or not isinstance(endpoint_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(endpoint_id) is None
            or not isinstance(raw_mac, str)
            or re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", raw_mac) is None
            or not isinstance(raw_ipv4, str)
            or endpoint.get("IPv6Address") != ""
        ):
            raise DeploymentError("candidate network endpoint identity is invalid")
        try:
            interface = ip_interface(raw_ipv4)
        except ValueError:
            raise DeploymentError(
                "candidate network endpoint identity is invalid"
            ) from None
        if (
            not isinstance(interface, IPv4Interface)
            or interface.network != subnet
            or interface.ip
            in {
                subnet.network_address,
                gateway,
                subnet.broadcast_address,
            }
        ):
            raise DeploymentError("candidate network endpoint identity is invalid")
        ipv4_address = str(interface)
    return CandidateNetworkContract(
        network_id=network_id,
        subnet=str(subnet),
        gateway=str(gateway),
        endpoint_id=endpoint_id,
        ipv4_address=ipv4_address,
    )


def _wait_for_healthy(
    runner: CommandRunner,
    container_name: str,
    *,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _parse_json(
            runner.run(
                [
                    DOCKER_BINARY,
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    container_name,
                ]
            ).stdout,
            label="candidate container state",
        )
        if isinstance(state, dict):
            health = state.get("Health")
            if _container_state_is_running_and_healthy(state):
                return
            if isinstance(health, dict) and health.get("Status") == "unhealthy":
                raise DeploymentError("candidate container healthcheck failed")
            if (
                state.get("Status") in {"dead", "exited", "removing"}
                or state.get("Dead") is True
                or state.get("Paused") is True
            ):
                raise DeploymentError(
                    "candidate container exited before becoming healthy"
                )
        time.sleep(0.5)
    raise DeploymentError(
        "candidate container did not become healthy within 120 seconds"
    )


def _container_state_is_running_and_healthy(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    health = value.get("Health")
    return (
        value.get("Status") == "running"
        and value.get("Running") is True
        and value.get("Paused") is False
        and value.get("Restarting") is False
        and value.get("Dead") is False
        and isinstance(health, dict)
        and health.get("Status") == "healthy"
    )


def _has_expected_candidate_tmpfs(raw_tmpfs: object) -> bool:
    if not isinstance(raw_tmpfs, dict) or set(raw_tmpfs) != {"/tmp"}:
        return False
    raw_options = raw_tmpfs.get("/tmp")
    if not isinstance(raw_options, str):
        return False

    flags: set[str] = set()
    values: dict[str, str] = {}
    for option in raw_options.split(","):
        if not option:
            return False
        if "=" not in option:
            if option in flags:
                return False
            flags.add(option)
            continue
        name, value = option.split("=", 1)
        if not name or not value or name in values:
            return False
        values[name] = value

    # Docker retains Compose's ``64m`` on current releases, while some API
    # paths normalize the same limit to bytes. These are the only accepted
    # representations; option order is not significant in inspect output.
    size_is_64_mib = values.get("size") in {"64m", "67108864"}
    return (
        flags == {"rw", "noexec", "nosuid", "nodev"}
        and values.keys() == {"size", "uid", "gid", "mode"}
        and size_is_64_mib
        and values["uid"] == str(RUNTIME_UID)
        and values["gid"] == str(RUNTIME_GID)
        and values["mode"] == "1777"
    )


def _assert_safe_host_config_defaults(host_config: Mapping[str, object]) -> None:
    unexpected_fields = set(host_config) - HOST_CONFIG_ALLOWED_FIELDS
    missing_fields = HOST_CONFIG_REQUIRED_SAFE_FIELDS - set(host_config)
    if unexpected_fields or missing_fields:
        raise DeploymentError(
            "candidate container host configuration field set is invalid"
        )

    exact_defaults: dict[str, object] = {
        "AutoRemove": False,
        "Binds": None,
        "BlkioWeight": 0,
        "Cgroup": "",
        "CgroupParent": "",
        "ContainerIDFile": "",
        "CpuCount": 0,
        "CpuPercent": 0,
        "CpuPeriod": 0,
        "CpuQuota": 0,
        "CpuRealtimePeriod": 0,
        "CpuRealtimeRuntime": 0,
        "CpuShares": 0,
        "CpusetCpus": "",
        "CpusetMems": "",
        "DeviceCgroupRules": None,
        "Dns": None,
        "DnsOptions": None,
        "DnsSearch": None,
        "GroupAdd": None,
        "IOMaximumBandwidth": 0,
        "IOMaximumIOps": 0,
        "Isolation": "",
        "KernelMemoryTCP": 0,
        "Links": None,
        "MemoryReservation": 0,
        "MemorySwap": 1024 * 1024 * 1024,
        "MemorySwappiness": None,
        "OomScoreAdj": 0,
        "PublishAllPorts": False,
        "Runtime": "runc",
        "ShmSize": 64 * 1024 * 1024,
        "StorageOpt": None,
        "Sysctls": None,
        "Ulimits": None,
        "VolumeDriver": "",
    }
    for name, expected_value in exact_defaults.items():
        if name in host_config and host_config[name] != expected_value:
            raise DeploymentError(
                f"candidate container host configuration has unsafe {name}"
            )

    nullable_empty_defaults = {
        "Annotations",
        "BlkioDeviceReadBps",
        "BlkioDeviceReadIOps",
        "BlkioDeviceWriteBps",
        "BlkioDeviceWriteIOps",
        "BlkioWeightDevice",
        "ExtraHosts",
    }
    for name in nullable_empty_defaults:
        if name in host_config and host_config[name] not in (None, [], {}):
            raise DeploymentError(
                f"candidate container host configuration has unsafe {name}"
            )
    if "OomKillDisable" in host_config and host_config["OomKillDisable"] not in (
        None,
        False,
    ):
        raise DeploymentError(
            "candidate container host configuration has unsafe OomKillDisable"
        )
    if "ConsoleSize" in host_config and host_config["ConsoleSize"] not in (
        [0, 0],
        (0, 0),
    ):
        raise DeploymentError(
            "candidate container host configuration has unsafe ConsoleSize"
        )

    for name, expected_paths in (
        ("MaskedPaths", REQUIRED_MASKED_PATHS),
        ("ReadonlyPaths", REQUIRED_READONLY_PATHS),
    ):
        raw_paths = host_config.get(name)
        if (
            not isinstance(raw_paths, list)
            or len(raw_paths) != len(expected_paths)
            or any(not isinstance(path, str) for path in raw_paths)
            or frozenset(raw_paths) != expected_paths
        ):
            raise DeploymentError(
                f"candidate container host configuration has unsafe {name}"
            )


def _candidate_runtime_sha256(container: Mapping[str, object]) -> str:
    projection = {
        "schema": 1,
        "path": container.get("Path"),
        "args": container.get("Args"),
        "config": container.get("Config"),
        "host_config": container.get("HostConfig"),
        "mounts": container.get("Mounts"),
        "apparmor_profile": container.get("AppArmorProfile"),
        "driver": container.get("Driver"),
        "platform": container.get("Platform"),
        "process_label": container.get("ProcessLabel"),
        "mount_label": container.get("MountLabel"),
    }
    try:
        canonical = json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError):
        raise DeploymentError(
            "candidate runtime identity is not canonical JSON"
        ) from None
    return hashlib.sha256(RUNTIME_HASH_DOMAIN + canonical).hexdigest()


def _upstream_identity_from_inspection(
    runner: CommandRunner,
    inspection: object,
    *,
    expected_container_id: str,
    expected_container_name: str,
    expected_host_port: int,
) -> UpstreamIdentity:
    if (
        not isinstance(inspection, dict)
        or not isinstance(expected_container_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None
        or inspection.get("Id") != expected_container_id
        or inspection.get("Name") != f"/{expected_container_name}"
    ):
        raise DeploymentError("upstream container identity is invalid")
    validated_port = validate_candidate_port(str(expected_host_port))
    image_id = inspection.get("Image")
    container_state = inspection.get("State")
    config = inspection.get("Config")
    host_config = inspection.get("HostConfig")
    mounts = inspection.get("Mounts")
    network_settings = inspection.get("NetworkSettings")
    if (
        not isinstance(image_id, str)
        or IMAGE_ID_PATTERN.fullmatch(image_id) is None
        or not _container_state_is_running_and_healthy(container_state)
        or not isinstance(config, dict)
        or not isinstance(host_config, dict)
        or not isinstance(mounts, list)
        or not isinstance(network_settings, dict)
    ):
        raise DeploymentError(
            "upstream container must be running and healthy with complete identity"
        )
    image_reference = config.get("Image")
    labels = config.get("Labels")
    source_sha = (
        labels.get("org.opencontainers.image.revision")
        if isinstance(labels, dict)
        else None
    )
    if (
        not isinstance(image_reference, str)
        or not image_reference
        or not isinstance(source_sha, str)
        or SOURCE_SHA_PATTERN.fullmatch(source_sha) is None
    ):
        raise DeploymentError("upstream container image identity is invalid")

    bindings = host_config.get("PortBindings")
    expected_binding = {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(validated_port)}]
    }
    if bindings != expected_binding:
        raise DeploymentError("upstream container must expose one loopback port")

    if len(mounts) != 1 or not isinstance(mounts[0], dict):
        raise DeploymentError("upstream container data mount is invalid")
    mount = mounts[0]
    if (
        set(mount) != {"Type", "Source", "Destination", "Mode", "Propagation", "RW"}
        or mount.get("Type") != "bind"
        or mount.get("Destination") != "/app/data"
        or mount.get("Mode") != ""
        or mount.get("Propagation") != "rprivate"
        or mount.get("RW") is not True
        or not isinstance(mount.get("Source"), str)
        or not Path(cast(str, mount["Source"])).is_absolute()
    ):
        raise DeploymentError("upstream container data mount is invalid")
    data_path = Path(cast(str, mount["Source"]))
    data_identity = _data_directory_identity(data_path)
    if data_identity[2:] != (RUNTIME_UID, RUNTIME_GID):
        raise DeploymentError("upstream data directory ownership is invalid")
    _assert_container_data_mount_identity(
        runner,
        expected_container_id,
        data_identity,
    )

    networks = network_settings.get("Networks")
    if not isinstance(networks, dict) or len(networks) != 1:
        raise DeploymentError("upstream container network identity is invalid")
    network_name, endpoint = next(iter(networks.items()))
    if not isinstance(network_name, str) or not isinstance(endpoint, dict):
        raise DeploymentError("upstream container network identity is invalid")
    network_id = endpoint.get("NetworkID")
    endpoint_id = endpoint.get("EndpointID")
    if (
        not isinstance(network_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(network_id) is None
        or not isinstance(endpoint_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(endpoint_id) is None
    ):
        raise DeploymentError("upstream container network identity is invalid")
    network_inspection = _parse_json(
        runner.run([DOCKER_BINARY, "network", "inspect", network_name]).stdout,
        label="upstream network inspection",
    )
    if (
        not isinstance(network_inspection, list)
        or len(network_inspection) != 1
        or not isinstance(network_inspection[0], dict)
        or network_inspection[0].get("Name") != network_name
        or network_inspection[0].get("Id") != network_id
    ):
        raise DeploymentError("upstream network identity is inconsistent")
    network_containers = network_inspection[0].get("Containers")
    network_endpoint = (
        network_containers.get(expected_container_id)
        if isinstance(network_containers, dict)
        else None
    )
    if (
        not isinstance(network_containers, dict)
        or set(network_containers) != {expected_container_id}
        or not isinstance(network_endpoint, dict)
        or network_endpoint.get("Name") != expected_container_name
        or network_endpoint.get("EndpointID") != endpoint_id
    ):
        raise DeploymentError("upstream network endpoint identity is inconsistent")

    return _validate_upstream_identity_payload(
        {
            "schema": UPSTREAM_IDENTITY_SCHEMA,
            "docker_daemon_id": _local_docker_daemon_id(runner),
            "container_id": expected_container_id,
            "container_name": expected_container_name,
            "image_id": image_id,
            "image_reference": image_reference,
            "source_sha": source_sha,
            "host_port": validated_port,
            "data_path": str(data_path),
            "data_device": data_identity[0],
            "data_inode": data_identity[1],
            "network_name": network_name,
            "network_id": network_id,
            "network_endpoint_id": endpoint_id,
            "runtime_sha256": _candidate_runtime_sha256(inspection),
        }
    )


def _assert_upstream_identity_current(
    runner: CommandRunner,
    expected: UpstreamIdentity,
) -> None:
    validated_expected = _validate_upstream_identity_payload(
        _upstream_identity_payload(expected)
    )
    inspection = _parse_json(
        runner.run([DOCKER_BINARY, "inspect", validated_expected.container_id]).stdout,
        label="upstream container inspection",
    )
    if (
        not isinstance(inspection, list)
        or len(inspection) != 1
        or not isinstance(inspection[0], dict)
    ):
        raise DeploymentError("upstream container inspection is invalid")
    current = _upstream_identity_from_inspection(
        runner,
        inspection[0],
        expected_container_id=validated_expected.container_id,
        expected_container_name=validated_expected.container_name,
        expected_host_port=validated_expected.host_port,
    )
    if current != validated_expected:
        raise DeploymentError("upstream identity changed after route verification")


def _assert_upstream_ready(
    runner: CommandRunner,
    expected: UpstreamIdentity,
) -> None:
    """Require a healthy, identity-bound target before exposing its route."""

    validated_expected = _validate_upstream_identity_payload(
        _upstream_identity_payload(expected)
    )
    _wait_for_healthy(
        runner,
        validated_expected.container_name,
        timeout=15.0,
    )
    _assert_upstream_identity_current(runner, validated_expected)


def _upstream_identity_for_host_port(
    runner: CommandRunner,
    host_port: int,
) -> UpstreamIdentity:
    validated_port = validate_candidate_port(str(host_port))
    expected_binding = {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(validated_port)}]
    }
    identifiers = runner.run(
        [DOCKER_BINARY, "container", "ls", "--all", "--quiet", "--no-trunc"]
    ).stdout.split()
    matches: list[UpstreamIdentity] = []
    for identifier in identifiers:
        if CONTAINER_ID_PATTERN.fullmatch(identifier) is None:
            raise DeploymentError("Docker returned an invalid container identity")
        inspection = _parse_json(
            runner.run([DOCKER_BINARY, "inspect", identifier]).stdout,
            label="upstream container inspection",
        )
        if (
            not isinstance(inspection, list)
            or len(inspection) != 1
            or not isinstance(inspection[0], dict)
        ):
            raise DeploymentError("upstream container inspection is invalid")
        container = inspection[0]
        host_config = container.get("HostConfig")
        if not isinstance(host_config, dict):
            raise DeploymentError("upstream container host configuration is invalid")
        if host_config.get("PortBindings") != expected_binding:
            continue
        raw_name = container.get("Name")
        if (
            not isinstance(raw_name, str)
            or not raw_name.startswith("/")
            or len(raw_name) == 1
        ):
            raise DeploymentError("upstream container name is invalid")
        matches.append(
            _upstream_identity_from_inspection(
                runner,
                container,
                expected_container_id=identifier,
                expected_container_name=raw_name[1:],
                expected_host_port=validated_port,
            )
        )
    if len(matches) != 1:
        raise DeploymentError(
            "managed Caddy upstream must identify exactly one Docker container"
        )
    return matches[0]


def validate_candidate_runtime_inspection(
    container: object,
    *,
    identity: CandidateIdentity,
    expected_image_id: str,
    network_name: str,
    port: int,
    data_directory: Path,
    trusted_proxy_json: str,
    image_config: Mapping[str, Any],
) -> None:
    if not isinstance(container, dict):
        raise DeploymentError(
            "candidate container inspection returned an invalid object"
        )
    config = container.get("Config")
    host_config = container.get("HostConfig")
    mounts = container.get("Mounts")
    if (
        not isinstance(config, dict)
        or not isinstance(host_config, dict)
        or not isinstance(mounts, list)
    ):
        raise DeploymentError("candidate container configuration is incomplete")

    _assert_safe_host_config_defaults(host_config)

    if container.get("Image") != expected_image_id:
        raise DeploymentError("candidate container uses the wrong immutable image ID")
    if config.get("Image") != identity.image:
        raise DeploymentError("candidate container uses the wrong image reference")
    if config.get("User") != f"{RUNTIME_UID}:{RUNTIME_GID}":
        raise DeploymentError("candidate container uses the wrong runtime user")
    baseline_fields = (
        "Cmd",
        "Healthcheck",
        "WorkingDir",
        "Volumes",
        "ExposedPorts",
    )
    if (
        any(
            field not in config
            or field not in image_config
            or config[field] != image_config[field]
            for field in baseline_fields
        )
        or "Entrypoint" not in config
        or config["Entrypoint"] is not None
        or image_config.get("Entrypoint") is not None
    ):
        raise DeploymentError(
            "candidate container overrides the immutable image configuration"
        )
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise DeploymentError("candidate container is missing image and Compose labels")
    if labels.get("org.opencontainers.image.revision") != identity.source_sha:
        raise DeploymentError(
            "candidate container OCI revision does not match SOURCE_SHA"
        )
    if labels.get("com.docker.compose.project") != identity.project_name:
        raise DeploymentError("candidate container has the wrong Compose project label")
    if labels.get("com.docker.compose.service") != COMPOSE_SERVICE:
        raise DeploymentError("candidate container has the wrong Compose service label")

    def parse_environment(raw: object, *, label: str) -> dict[str, str]:
        if not isinstance(raw, list):
            raise DeploymentError(f"{label} environment is incomplete")
        parsed: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, str) or "=" not in entry:
                raise DeploymentError(f"{label} environment is invalid")
            name, value = entry.split("=", 1)
            if not name or name in parsed:
                raise DeploymentError(f"{label} environment is ambiguous")
            parsed[name] = value
        return parsed

    environment = parse_environment(config.get("Env"), label="candidate container")
    baseline_environment = parse_environment(
        image_config.get("Env"), label="candidate image"
    )
    if baseline_environment.get("COMMERCE_OPS_SOURCE_SHA") != identity.source_sha:
        raise DeploymentError(
            "candidate image runtime source SHA does not match SOURCE_SHA"
        )
    if environment.get("COMMERCE_OPS_SOURCE_SHA") != identity.source_sha:
        raise DeploymentError(
            "candidate container runtime source SHA does not match SOURCE_SHA"
        )
    expected_environment = dict(baseline_environment)
    expected_environment.update(
        {
            "COMMERCE_OPS_ENVIRONMENT": "demo",
            "COMMERCE_OPS_ALLOWED_HOSTS": (
                '["commerce-ops-desk.srrsh.aig.rest","127.0.0.1","localhost"]'
            ),
            "COMMERCE_OPS_TRUSTED_PROXY_CIDRS": trusted_proxy_json,
            "COMMERCE_OPS_COOKIE_SECURE": "true",
            "COMMERCE_OPS_WEBHOOK_ENABLED": "true",
            "PORT": "8000",
        }
    )
    required_environment = {
        "COMMERCE_OPS_DATABASE_URL": "sqlite+pysqlite:////app/data/commerce_ops.db",
        "COMMERCE_OPS_ENVIRONMENT": "demo",
        "COMMERCE_OPS_VENV_DIR": "/opt/venv",
        "COMMERCE_OPS_ALLOWED_HOSTS": (
            '["commerce-ops-desk.srrsh.aig.rest","127.0.0.1","localhost"]'
        ),
        "COMMERCE_OPS_TRUSTED_PROXY_CIDRS": trusted_proxy_json,
        "COMMERCE_OPS_COOKIE_SECURE": "true",
        "COMMERCE_OPS_WEBHOOK_ENABLED": "true",
        "PORT": "8000",
    }
    if environment != expected_environment or any(
        environment.get(name) != value for name, value in required_environment.items()
    ):
        raise DeploymentError(
            "candidate container environment does not match the image baseline"
        )

    if host_config.get("Privileged") is not False:
        raise DeploymentError("candidate container must not be privileged")
    if host_config.get("ReadonlyRootfs") is not True:
        raise DeploymentError("candidate container root filesystem must be read-only")
    if host_config.get("CapDrop") != ["ALL"]:
        raise DeploymentError("candidate container must drop all capabilities")
    expected_isolation: dict[str, object] = {
        "CapAdd": None,
        "DeviceRequests": None,
        "VolumesFrom": None,
        "PidMode": "",
        "IpcMode": "private",
        "UTSMode": "",
        "UsernsMode": "",
        "CgroupnsMode": "private",
        "NetworkMode": network_name,
    }
    if (
        "Devices" not in host_config
        or host_config["Devices"] not in (None, [])
        or any(
            name not in host_config or host_config[name] != expected_value
            for name, expected_value in expected_isolation.items()
        )
    ):
        raise DeploymentError(
            "candidate container has an unexpected privilege or namespace isolation setting"
        )
    security_options = host_config.get("SecurityOpt")
    if security_options not in (
        ["no-new-privileges:true"],
        ["no-new-privileges"],
    ):
        raise DeploymentError("candidate container must enforce no-new-privileges")
    if host_config.get("Init") is not True:
        raise DeploymentError("candidate container must use an init process")
    if host_config.get("RestartPolicy") != {
        "Name": "unless-stopped",
        "MaximumRetryCount": 0,
    }:
        raise DeploymentError("candidate container restart policy is incorrect")

    bindings = host_config.get("PortBindings")
    expected_binding = {"HostIp": "127.0.0.1", "HostPort": str(port)}
    if bindings != {"8000/tcp": [expected_binding]}:
        raise DeploymentError("candidate container port is not bound only to loopback")
    if not _has_expected_candidate_tmpfs(host_config.get("Tmpfs")):
        raise DeploymentError("candidate container tmpfs configuration is incorrect")
    if host_config.get("NanoCpus") != 1_000_000_000:
        raise DeploymentError("candidate container CPU limit is incorrect")
    if host_config.get("Memory") != 512 * 1024 * 1024:
        raise DeploymentError("candidate container memory limit is incorrect")
    if host_config.get("PidsLimit") != 128:
        raise DeploymentError("candidate container PID limit is incorrect")
    log_config = host_config.get("LogConfig")
    if not isinstance(log_config, dict) or log_config.get("Type") != "local":
        raise DeploymentError("candidate container log driver is incorrect")
    if log_config.get("Config") != {"max-size": "10m", "max-file": "3"}:
        raise DeploymentError("candidate container log rotation is incorrect")

    if len(mounts) != 1 or not isinstance(mounts[0], dict):
        raise DeploymentError("candidate container must have only one data mount")
    mount = mounts[0]
    if (
        set(mount) != {"Type", "Source", "Destination", "Mode", "Propagation", "RW"}
        or mount.get("Type") != "bind"
        or mount.get("Source") != str(data_directory)
        or mount.get("Destination") != "/app/data"
        or mount.get("Mode") != ""
        or mount.get("Propagation") != "rprivate"
        or mount.get("RW") is not True
    ):
        raise DeploymentError("candidate container uses the wrong data mount")

    expected_network_name = f"{identity.project_name}_default"
    if network_name != expected_network_name:
        raise DeploymentError("candidate state contains the wrong Compose network name")
    network_settings = container.get("NetworkSettings")
    networks = (
        network_settings.get("Networks") if isinstance(network_settings, dict) else None
    )
    if not isinstance(networks, dict) or set(networks) != {network_name}:
        raise DeploymentError("candidate container is attached to the wrong network")
    network = networks[network_name]
    if not isinstance(network, dict):
        raise DeploymentError("candidate container network inspection is incomplete")
    try:
        trusted_entries = json.loads(trusted_proxy_json)
    except json.JSONDecodeError:
        raise DeploymentError("candidate trusted proxy contract is invalid") from None
    if not isinstance(trusted_entries, list) or len(trusted_entries) != 1:
        raise DeploymentError("candidate trusted proxy contract is invalid")
    try:
        trusted_network = ip_network(trusted_entries[0], strict=True)
    except (TypeError, ValueError):
        raise DeploymentError("candidate trusted proxy contract is invalid") from None
    if trusted_network.version != 4 or trusted_network.prefixlen != 32:
        raise DeploymentError("candidate trusted proxy contract is invalid")
    if network.get("Gateway") != str(trusted_network.network_address):
        raise DeploymentError("candidate container has the wrong network gateway")


def _assert_candidate_runtime(
    runner: CommandRunner,
    identity: CandidateIdentity,
    *,
    expected_container_id: str,
    expected_image_id: str,
    network_name: str,
    port: int,
    data_directory: Path,
    data_identity: DataDirectoryIdentity,
    trusted_proxy_json: str,
    image_config: Mapping[str, Any],
) -> str:
    if CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None:
        raise DeploymentError("candidate container ID is invalid")
    inspection = _parse_json(
        runner.run([DOCKER_BINARY, "inspect", expected_container_id]).stdout,
        label="candidate container inspection",
    )
    if (
        not isinstance(inspection, list)
        or len(inspection) != 1
        or not isinstance(inspection[0], dict)
        or inspection[0].get("Id") != expected_container_id
        or inspection[0].get("Name") != f"/{identity.container_name}"
    ):
        raise DeploymentError(
            "candidate container inspection returned the wrong identity"
        )
    validate_candidate_runtime_inspection(
        inspection[0],
        identity=identity,
        expected_image_id=expected_image_id,
        network_name=network_name,
        port=port,
        data_directory=data_directory,
        trusted_proxy_json=trusted_proxy_json,
        image_config=image_config,
    )
    _assert_container_data_mount_identity(
        runner,
        expected_container_id,
        data_identity,
    )
    return _candidate_runtime_sha256(inspection[0])


def _validate_private_directory(directory: Path, *, label: str) -> Path:
    if not directory.is_absolute():
        raise DeploymentError(f"{label} must be an absolute path")
    try:
        metadata = directory.lstat()
        resolved = directory.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise DeploymentError(f"{label} does not exist") from None
    if stat.S_ISLNK(metadata.st_mode) or resolved != directory:
        raise DeploymentError(f"{label} must not contain a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise DeploymentError(f"{label} must be a directory")
    if metadata.st_uid != os.geteuid():
        raise DeploymentError(f"{label} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DeploymentError(f"{label} must have mode 0700")
    return resolved


def _prepare_private_directory(directory: Path, *, label: str) -> Path:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
    except OSError:
        raise DeploymentError(f"{label} could not be created safely") from None
    if stat.S_ISLNK(metadata.st_mode) or directory.resolve(strict=True) != directory:
        raise DeploymentError(f"{label} must not contain a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise DeploymentError(f"{label} must be owned by the current user")
    try:
        directory.chmod(0o700)
    except OSError:
        raise DeploymentError(f"{label} permissions could not be secured") from None
    return _validate_private_directory(directory, label=label)


def _read_private_text(path: Path, *, directory: Path, label: str) -> str:
    private_directory = _validate_private_directory(
        directory, label=f"{label} directory"
    )
    if not path.is_absolute() or path.parent != directory:
        raise DeploymentError(f"{label} must be directly inside {directory}")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise DeploymentError(f"{label} does not exist") from None
    if stat.S_ISLNK(metadata.st_mode) or resolved != path:
        raise DeploymentError(f"{label} must not be a symbolic link")
    if resolved.parent != private_directory or not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError(f"{label} must be a regular file in {directory}")
    if metadata.st_uid != os.geteuid():
        raise DeploymentError(f"{label} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DeploymentError(f"{label} must have mode 0600")
    if metadata.st_nlink != 1:
        raise DeploymentError(f"{label} must have exactly one hard link")
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise DeploymentError(f"{label} could not be read safely") from None


def _validate_manifest_remote_ref(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("refs/remotes/"):
        raise DeploymentError("build manifest has an invalid approved remote ref")
    suffix = value.removeprefix("refs/remotes/")
    if (
        not suffix
        or "/" not in suffix
        or ".." in suffix
        or "//" in suffix
        or "@{" in suffix
        or value.endswith(("/", ".", ".lock"))
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in "~^:?*[\\" for character in value)
    ):
        raise DeploymentError("build manifest has an invalid approved remote ref")
    return value


def _validate_docker_daemon_id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise DeploymentError(f"{label} has an invalid Docker daemon ID")
    return value


def _validate_deployment_assets(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "sha256"}:
        raise DeploymentError("deployment asset manifest schema is invalid")
    if (
        type(value.get("schema")) is not int
        or value["schema"] != DEPLOYMENT_ASSET_SCHEMA
    ):
        raise DeploymentError("deployment asset manifest schema is unsupported")
    digests = value.get("sha256")
    if not isinstance(digests, dict) or set(digests) != set(DEPLOYMENT_ASSET_PATHS):
        raise DeploymentError("deployment asset manifest path set is invalid")
    if any(
        not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
        for digest in digests.values()
    ):
        raise DeploymentError("deployment asset manifest digest is invalid")
    return cast(dict[str, Any], value)


def _read_verified_deployment_asset(
    relative_path: str,
    *,
    expected_sha256: str,
) -> str:
    try:
        product_root = PRODUCT_ROOT.resolve(strict=True)
        path = product_root / relative_path
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise DeploymentError(
            f"deployment asset is unavailable: {relative_path}"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or resolved != path
        or not resolved.is_relative_to(product_root)
    ):
        raise DeploymentError(
            f"deployment asset is not a canonical regular file: {relative_path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise DeploymentError(
                    f"deployment asset is not a regular file: {relative_path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                content = source.read(MAX_DEPLOYMENT_ASSET_BYTES + 1)
        finally:
            os.close(descriptor)
    except DeploymentError:
        raise
    except OSError:
        raise DeploymentError(
            f"deployment asset could not be read safely: {relative_path}"
        ) from None
    if len(content) > MAX_DEPLOYMENT_ASSET_BYTES:
        raise DeploymentError(f"deployment asset is too large: {relative_path}")
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != expected_sha256:
        raise DeploymentError(f"deployment asset digest changed: {relative_path}")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise DeploymentError(
            f"deployment asset is not UTF-8: {relative_path}"
        ) from None


def _load_verified_deployment_assets(value: object) -> VerifiedDeploymentAssets:
    validated = _validate_deployment_assets(value)
    raw_digests = cast(dict[str, str], validated["sha256"])
    contents = {
        relative_path: _read_verified_deployment_asset(
            relative_path,
            expected_sha256=raw_digests[relative_path],
        )
        for relative_path in DEPLOYMENT_ASSET_PATHS
    }
    identity: dict[str, object] = {
        "schema": DEPLOYMENT_ASSET_SCHEMA,
        "sha256": dict(raw_digests),
    }
    caddy_templates = {
        profile: contents[relative_path]
        for profile, relative_path in CADDY_PROFILE_ASSET_PATHS.items()
    }
    return VerifiedDeploymentAssets(
        identity=MappingProxyType(identity),
        compose_yaml=contents[COMPOSE_ASSET_PATH],
        caddy_templates=MappingProxyType(caddy_templates),
    )


def _deployment_asset_identity_payload(
    assets: VerifiedDeploymentAssets,
) -> dict[str, object]:
    raw_digests = assets.identity.get("sha256")
    if not isinstance(
        raw_digests, Mapping
    ):  # pragma: no cover - constructor invariant.
        raise DeploymentError("verified deployment asset identity is invalid")
    return {
        "schema": DEPLOYMENT_ASSET_SCHEMA,
        "sha256": dict(raw_digests),
    }


def _load_build_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            _read_private_text(
                path,
                directory=STATE_DIRECTORY,
                label="verified build manifest",
            )
        )
    except json.JSONDecodeError:
        raise DeploymentError("verified build manifest is invalid") from None
    expected_fields = {
        "approved_remote_ref",
        "deployment_assets",
        "docker_daemon_id",
        "image_id",
        "image_reference",
        "schema",
        "source_sha",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise DeploymentError("verified build manifest schema is invalid")
    if (
        type(payload.get("schema")) is not int
        or payload["schema"] != BUILD_MANIFEST_SCHEMA
    ):
        raise DeploymentError("verified build manifest schema is unsupported")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str):
        raise DeploymentError("verified build manifest is missing SOURCE_SHA")
    identity = candidate_identity(source_sha)
    if payload.get("image_reference") != identity.image:
        raise DeploymentError("verified build manifest has an invalid image reference")
    image_id = payload.get("image_id")
    if not isinstance(image_id, str) or IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise DeploymentError("verified build manifest has an invalid image ID")
    _validate_manifest_remote_ref(payload.get("approved_remote_ref"))
    _validate_deployment_assets(payload.get("deployment_assets"))
    _validate_docker_daemon_id(
        payload.get("docker_daemon_id"),
        label="verified build manifest",
    )
    return cast(dict[str, Any], payload)


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    directory = _prepare_private_directory(
        path.parent, label="candidate state directory"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        try:
            descriptor = os.open(temporary, flags, 0o600)
            created = True
        except OSError:
            raise DeploymentError(
                "candidate state could not be written safely"
            ) from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, path)
            created = False
            directory_descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            raise DeploymentError(
                "candidate state could not be written safely"
            ) from None
    finally:
        if created:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def prepare_candidate(arguments: argparse.Namespace, runner: CommandRunner) -> Path:
    build_manifest = _load_build_manifest(Path(arguments.build_manifest))
    deployment_assets = _load_verified_deployment_assets(
        build_manifest["deployment_assets"]
    )
    identity = candidate_identity(cast(str, build_manifest["source_sha"]))
    port = validate_candidate_port(arguments.candidate_port)
    live, candidate = validate_data_directories(
        app_root=APP_ROOT,
        live_data_dir=Path(arguments.live_data_dir),
        candidate_data_dir=Path(arguments.candidate_data_dir),
        source_sha=identity.source_sha,
    )
    validate_deployment_layout(
        product_root=PRODUCT_ROOT,
        app_root=APP_ROOT,
        state_directory=STATE_DIRECTORY,
        live_data_dir=live,
        candidate_data_dir=candidate,
    )
    live_data_identity = _data_directory_identity(live)
    candidate_data_identity = _data_directory_identity(candidate)
    docker_daemon_id = cast(str, build_manifest["docker_daemon_id"])
    _assert_local_docker_daemon(runner, docker_daemon_id)
    expected_image_id = cast(str, build_manifest["image_id"])
    _assert_image_revision(
        runner,
        identity,
        expected_image_id=expected_image_id,
    )
    image_config = _immutable_image_config(runner, identity, expected_image_id)
    _assert_candidate_absent(runner, identity)
    _assert_port_available(runner, port)

    placeholder_proxy = '["127.0.0.1/32"]'
    placeholder_environment = _compose_environment(
        identity,
        port=port,
        data_directory=candidate,
        trusted_proxy_json=placeholder_proxy,
    )
    _assert_compose_service_contract(
        runner,
        identity,
        compose_yaml=deployment_assets.compose_yaml,
        environment=placeholder_environment,
    )
    runner.run(
        _compose_command(
            identity,
            "create",
            "--no-build",
            "--no-deps",
            COMPOSE_SERVICE,
        ),
        environment=placeholder_environment,
        input_text=deployment_assets.compose_yaml,
    )
    _assert_exact_project_container(runner, identity)
    network_name, network_inspection = _created_project_network(runner, identity)
    prepared_network = _validate_candidate_network_contract(
        identity,
        network_name,
        network_inspection,
        expected_container_id=None,
    )
    trusted_proxy_json = prepared_network.trusted_proxy_json

    final_environment = _compose_environment(
        identity,
        port=port,
        data_directory=candidate,
        trusted_proxy_json=trusted_proxy_json,
    )
    runner.run(
        _compose_command(
            identity,
            "up",
            "--detach",
            "--force-recreate",
            "--no-build",
            "--no-deps",
            COMPOSE_SERVICE,
        ),
        environment=final_environment,
        input_text=deployment_assets.compose_yaml,
    )
    final_container_id = _assert_exact_project_container(runner, identity)
    final_network_inspection = _parse_json(
        runner.run([DOCKER_BINARY, "network", "inspect", network_name]).stdout,
        label="final candidate network inspection",
    )
    final_network = _validate_candidate_network_contract(
        identity,
        network_name,
        final_network_inspection,
        expected_container_id=final_container_id,
    )
    if (
        final_network.network_id != prepared_network.network_id
        or final_network.subnet != prepared_network.subnet
        or final_network.gateway != prepared_network.gateway
        or final_network.trusted_proxy_json != trusted_proxy_json
        or final_network.endpoint_id is None
    ):
        raise DeploymentError("candidate network identity changed during preparation")
    _wait_for_healthy(runner, identity.container_name)
    _assert_data_directory_identity(
        live,
        live_data_identity,
        label="live data directory",
    )
    _assert_data_directory_identity(
        candidate,
        candidate_data_identity,
        label="candidate data directory",
    )
    runtime_sha256 = _assert_candidate_runtime(
        runner,
        identity,
        expected_container_id=final_container_id,
        expected_image_id=expected_image_id,
        network_name=network_name,
        port=port,
        data_directory=candidate,
        data_identity=candidate_data_identity,
        trusted_proxy_json=trusted_proxy_json,
        image_config=image_config,
    )
    _assert_local_docker_daemon(runner, docker_daemon_id)

    state_path = STATE_DIRECTORY / f"candidate-{identity.source_sha[:12]}.json"
    _write_private_json(
        state_path,
        {
            "schema": CANDIDATE_STATE_SCHEMA,
            "source_sha": identity.source_sha,
            "project_name": identity.project_name,
            "container_name": identity.container_name,
            "container_id": final_container_id,
            "image": identity.image,
            "image_id": expected_image_id,
            "docker_daemon_id": docker_daemon_id,
            "candidate_port": port,
            "candidate_data_dir": str(candidate),
            "candidate_data_device": candidate_data_identity[0],
            "candidate_data_inode": candidate_data_identity[1],
            "live_data_dir": str(live),
            "live_data_device": live_data_identity[0],
            "live_data_inode": live_data_identity[1],
            "network_name": network_name,
            "network_id": final_network.network_id,
            "network_endpoint_id": final_network.endpoint_id,
            "runtime_sha256": runtime_sha256,
            "trusted_proxy_cidrs": json.loads(trusted_proxy_json),
            "deployment_assets": _deployment_asset_identity_payload(deployment_assets),
        },
    )
    return state_path


def _load_candidate_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            _read_private_text(
                path,
                directory=STATE_DIRECTORY,
                label="candidate state file",
            )
        )
    except json.JSONDecodeError:
        raise DeploymentError("candidate state file is invalid") from None
    expected_fields = {
        "schema",
        "source_sha",
        "project_name",
        "container_name",
        "container_id",
        "image",
        "image_id",
        "docker_daemon_id",
        "candidate_port",
        "candidate_data_dir",
        "candidate_data_device",
        "candidate_data_inode",
        "live_data_dir",
        "live_data_device",
        "live_data_inode",
        "network_name",
        "network_id",
        "network_endpoint_id",
        "runtime_sha256",
        "trusted_proxy_cidrs",
        "deployment_assets",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise DeploymentError("candidate state schema is invalid")
    if (
        type(payload.get("schema")) is not int
        or payload["schema"] != CANDIDATE_STATE_SCHEMA
    ):
        raise DeploymentError("candidate state schema is unsupported")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str):
        raise DeploymentError("candidate state is missing SOURCE_SHA")
    identity = candidate_identity(source_sha)
    expected = {
        "project_name": identity.project_name,
        "container_name": identity.container_name,
        "image": identity.image,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise DeploymentError(f"candidate state has an invalid {field}")
    image_id = payload.get("image_id")
    if not isinstance(image_id, str) or IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise DeploymentError("candidate state has an invalid immutable image ID")
    container_id = payload.get("container_id")
    if (
        not isinstance(container_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
    ):
        raise DeploymentError("candidate state has an invalid container ID")
    _validate_docker_daemon_id(
        payload.get("docker_daemon_id"),
        label="candidate state",
    )
    _validate_deployment_assets(payload.get("deployment_assets"))
    candidate_port = payload.get("candidate_port")
    if not isinstance(candidate_port, int) or isinstance(candidate_port, bool):
        raise DeploymentError("candidate state has an invalid candidate port")
    validate_candidate_port(str(candidate_port))
    for field in ("candidate_data_dir", "live_data_dir"):
        raw_path = payload.get(field)
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise DeploymentError(f"candidate state has an invalid {field}")
    for field in (
        "candidate_data_device",
        "candidate_data_inode",
        "live_data_device",
        "live_data_inode",
    ):
        identity_value = payload.get(field)
        if (
            type(identity_value) is not int
            or identity_value < 0
            or (field.endswith("_inode") and identity_value == 0)
        ):
            raise DeploymentError(f"candidate state has an invalid {field}")
    if (
        payload["candidate_data_device"],
        payload["candidate_data_inode"],
    ) == (payload["live_data_device"], payload["live_data_inode"]):
        raise DeploymentError("candidate state data directory identities overlap")
    if payload.get("network_name") != f"{identity.project_name}_default":
        raise DeploymentError("candidate state has an invalid network_name")
    for field in ("network_id", "network_endpoint_id"):
        network_identity = payload.get(field)
        if (
            not isinstance(network_identity, str)
            or CONTAINER_ID_PATTERN.fullmatch(network_identity) is None
        ):
            raise DeploymentError(f"candidate state has an invalid {field}")
    runtime_sha256 = payload.get("runtime_sha256")
    if (
        not isinstance(runtime_sha256, str)
        or SHA256_PATTERN.fullmatch(runtime_sha256) is None
    ):
        raise DeploymentError("candidate state has an invalid runtime_sha256")
    trusted_proxy_cidrs = payload.get("trusted_proxy_cidrs")
    if not isinstance(trusted_proxy_cidrs, list) or len(trusted_proxy_cidrs) != 1:
        raise DeploymentError("candidate state has invalid trusted proxy CIDRs")
    return cast(dict[str, Any], payload)


def _assert_state_candidate_ready(
    runner: CommandRunner,
    state: Mapping[str, Any],
) -> VerifiedCandidate:
    deployment_assets = _load_verified_deployment_assets(state.get("deployment_assets"))
    source_sha = cast(str, state["source_sha"])
    identity = candidate_identity(source_sha)
    port = validate_candidate_port(str(state.get("candidate_port", "")))
    candidate_data = Path(str(state.get("candidate_data_dir", "")))
    live_data = Path(str(state.get("live_data_dir", "")))
    live_data, candidate_data = validate_data_directories(
        app_root=APP_ROOT,
        live_data_dir=live_data,
        candidate_data_dir=candidate_data,
        source_sha=source_sha,
    )
    validate_deployment_layout(
        product_root=PRODUCT_ROOT,
        app_root=APP_ROOT,
        state_directory=STATE_DIRECTORY,
        live_data_dir=live_data,
        candidate_data_dir=candidate_data,
    )
    candidate_data_identity: DataDirectoryIdentity = (
        cast(int, state["candidate_data_device"]),
        cast(int, state["candidate_data_inode"]),
        RUNTIME_UID,
        RUNTIME_GID,
    )
    live_data_identity: DataDirectoryIdentity = (
        cast(int, state["live_data_device"]),
        cast(int, state["live_data_inode"]),
        RUNTIME_UID,
        RUNTIME_GID,
    )
    _assert_data_directory_identity(
        candidate_data,
        candidate_data_identity,
        label="candidate data directory",
    )
    _assert_data_directory_identity(
        live_data,
        live_data_identity,
        label="live data directory",
    )
    docker_daemon_id = cast(str, state["docker_daemon_id"])
    _assert_local_docker_daemon(runner, docker_daemon_id)
    expected_container_id = cast(str, state["container_id"])
    if _assert_exact_project_container(runner, identity) != expected_container_id:
        raise DeploymentError("candidate container changed after preparation")
    trusted_entries = state.get("trusted_proxy_cidrs")
    if not isinstance(trusted_entries, list) or len(trusted_entries) != 1:
        raise DeploymentError("candidate state must contain one trusted proxy /32")
    trusted_proxy_json = json.dumps(trusted_entries, separators=(",", ":"))
    try:
        trusted_network = ip_network(cast(str, trusted_entries[0]), strict=True)
    except (TypeError, ValueError):
        raise DeploymentError(
            "candidate state contains an invalid trusted proxy /32"
        ) from None
    if trusted_network.version != 4 or trusted_network.prefixlen != 32:
        raise DeploymentError("candidate state must contain one trusted proxy /32")
    network_name = state.get("network_name")
    if (
        not isinstance(network_name, str)
        or network_name != f"{identity.project_name}_default"
    ):
        raise DeploymentError("candidate state contains the wrong Compose network name")
    network_inspection = _parse_json(
        runner.run([DOCKER_BINARY, "network", "inspect", network_name]).stdout,
        label="candidate network inspection",
    )
    current_network = _validate_candidate_network_contract(
        identity,
        network_name,
        network_inspection,
        expected_container_id=expected_container_id,
    )
    if (
        current_network.trusted_proxy_json != trusted_proxy_json
        or current_network.network_id != state.get("network_id")
        or current_network.endpoint_id != state.get("network_endpoint_id")
    ):
        raise DeploymentError("candidate network identity changed after preparation")

    expected_image_id = cast(str, state["image_id"])
    _assert_image_revision(
        runner,
        identity,
        expected_image_id=expected_image_id,
    )
    image_config = _immutable_image_config(runner, identity, expected_image_id)
    _wait_for_healthy(runner, identity.container_name, timeout=15.0)
    runtime_sha256 = _assert_candidate_runtime(
        runner,
        identity,
        expected_container_id=expected_container_id,
        expected_image_id=expected_image_id,
        network_name=network_name,
        port=port,
        data_directory=candidate_data,
        data_identity=candidate_data_identity,
        trusted_proxy_json=trusted_proxy_json,
        image_config=image_config,
    )
    if runtime_sha256 != state.get("runtime_sha256"):
        raise DeploymentError("candidate runtime identity changed after preparation")
    _assert_local_docker_daemon(runner, docker_daemon_id)
    upstream = _validate_upstream_identity_payload(
        {
            "schema": UPSTREAM_IDENTITY_SCHEMA,
            "docker_daemon_id": docker_daemon_id,
            "container_id": expected_container_id,
            "container_name": identity.container_name,
            "image_id": expected_image_id,
            "image_reference": identity.image,
            "source_sha": identity.source_sha,
            "host_port": port,
            "data_path": str(candidate_data),
            "data_device": candidate_data_identity[0],
            "data_inode": candidate_data_identity[1],
            "network_name": network_name,
            "network_id": cast(str, state["network_id"]),
            "network_endpoint_id": cast(str, state["network_endpoint_id"]),
            "runtime_sha256": runtime_sha256,
        }
    )
    return VerifiedCandidate(
        deployment_assets=deployment_assets,
        upstream=upstream,
    )


def _write_private_text(path: Path, value: str) -> None:
    _prepare_private_directory(path.parent, label="private fragment directory")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _read_current_site(runner: CommandRunner) -> str:
    runner.run([SUDO_BINARY, "test", "-f", str(CADDY_SITE)])
    symlink_probe = runner.run(
        [SUDO_BINARY, "test", "-L", str(CADDY_SITE)],
        allowed_returncodes=frozenset({0, 1}),
    )
    if symlink_probe.returncode == 0:
        raise DeploymentError("CommerceOps Caddy site must not be a symbolic link")
    return runner.run([SUDO_BINARY, "cat", str(CADDY_SITE)]).stdout


def _require_caddy_mutation_fence_token(runner: CommandRunner) -> str:
    token = getattr(runner, "_caddy_mutation_fence_token", None)
    if not isinstance(token, str) or SHA256_PATTERN.fullmatch(token) is None:
        raise DeploymentError("Caddy mutation requires an active fence token")
    return token


def _change_caddy_mutation_fence(
    runner: CommandRunner,
    *,
    action: str,
    token: str,
) -> None:
    if action not in {"activate", "deactivate"}:
        raise DeploymentError("Caddy mutation fence action is invalid")
    if SHA256_PATTERN.fullmatch(token) is None:
        raise DeploymentError("Caddy mutation fence token is invalid")
    helper = """\
import fcntl
import os
import stat
import sys

action, token, lock_path, fence_path = sys.argv[1:]
if (
    action not in {"activate", "deactivate"}
    or len(token) != 64
    or any(character not in "0123456789abcdef" for character in token)
    or os.path.dirname(lock_path) != os.path.dirname(fence_path)
):
    raise SystemExit("invalid Caddy mutation fence request")
directory = os.path.dirname(lock_path)
directory_stat = os.lstat(directory)
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != 0
    or directory_stat.st_gid != 0
    or stat.S_IMODE(directory_stat.st_mode) & 0o002
):
    raise SystemExit("unsafe Caddy mutation lock directory")
directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
directory_flags |= getattr(os, "O_CLOEXEC", 0)
directory_flags |= getattr(os, "O_NOFOLLOW", 0)
directory_descriptor = os.open(directory, directory_flags)
lock_name = os.path.basename(lock_path)
fence_name = os.path.basename(fence_path)
lock_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
lock_flags |= getattr(os, "O_CLOEXEC", 0)
lock_flags |= getattr(os, "O_NOFOLLOW", 0)
try:
    try:
        lock_descriptor = os.open(
            lock_name,
            lock_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError:
        lock_stat = os.stat(
            lock_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != 0
            or lock_stat.st_gid != 0
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
            or lock_stat.st_nlink != 1
        ):
            raise SystemExit("unsafe existing Caddy mutation lock")
        existing_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        existing_flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_descriptor = os.open(
            lock_name,
            existing_flags,
            dir_fd=directory_descriptor,
        )
        opened_lock = os.fstat(lock_descriptor)
        if (
            (opened_lock.st_dev, opened_lock.st_ino)
            != (lock_stat.st_dev, lock_stat.st_ino)
            or not stat.S_ISREG(opened_lock.st_mode)
            or opened_lock.st_uid != 0
            or opened_lock.st_gid != 0
            or stat.S_IMODE(opened_lock.st_mode) != 0o600
            or opened_lock.st_nlink != 1
        ):
            raise SystemExit("Caddy mutation lock changed while opening")
    else:
        os.fchown(lock_descriptor, 0, 0)
        os.fchmod(lock_descriptor, 0o600)
        os.fsync(lock_descriptor)
        os.fsync(directory_descriptor)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if action == "activate":
            temporary = f".{fence_name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
            temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            temporary_flags |= getattr(os, "O_CLOEXEC", 0)
            temporary_flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_descriptor = os.open(
                temporary,
                temporary_flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            try:
                os.fchown(temporary_descriptor, 0, 0)
                os.fchmod(temporary_descriptor, 0o600)
                with os.fdopen(temporary_descriptor, "wb", closefd=False) as output:
                    output.write((token + "\\n").encode("ascii"))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(
                    temporary,
                    fence_name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            finally:
                os.close(temporary_descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
        else:
            fence_stat = os.stat(
                fence_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(fence_stat.st_mode)
                or fence_stat.st_uid != 0
                or fence_stat.st_gid != 0
                or stat.S_IMODE(fence_stat.st_mode) != 0o600
                or fence_stat.st_nlink != 1
            ):
                raise SystemExit("unsafe Caddy mutation fence")
            fence_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            fence_flags |= getattr(os, "O_NOFOLLOW", 0)
            fence_descriptor = os.open(
                fence_name,
                fence_flags,
                dir_fd=directory_descriptor,
            )
            try:
                opened_fence = os.fstat(fence_descriptor)
                if (
                    (opened_fence.st_dev, opened_fence.st_ino)
                    != (fence_stat.st_dev, fence_stat.st_ino)
                    or opened_fence.st_nlink != 1
                ):
                    raise SystemExit("Caddy mutation fence changed while opening")
                current = os.read(fence_descriptor, 66)
            finally:
                os.close(fence_descriptor)
            if current != (token + "\\n").encode("ascii"):
                raise SystemExit("Caddy mutation fence token changed")
            os.unlink(fence_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
finally:
    os.close(directory_descriptor)
"""
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            helper,
            action,
            token,
            str(CADDY_MUTATION_LOCK),
            str(CADDY_MUTATION_FENCE),
        ],
        timeout=None,
    )


def _activate_caddy_mutation_fence(runner: CommandRunner, token: str) -> None:
    _change_caddy_mutation_fence(runner, action="activate", token=token)


def _deactivate_caddy_mutation_fence(runner: CommandRunner, token: str) -> None:
    _change_caddy_mutation_fence(runner, action="deactivate", token=token)


@contextmanager
def _caddy_transaction_lock(runner: CommandRunner) -> Iterator[None]:
    """Serialize cooperating helper transactions with one root-owned advisory lock."""

    bootstrap = """\
import os
import stat
import sys

path = sys.argv[1]
deploy_gid = int(sys.argv[2])
nofollow = getattr(os, "O_NOFOLLOW", 0)
cloexec = getattr(os, "O_CLOEXEC", 0)
flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | cloexec | nofollow
try:
    descriptor = os.open(path, flags, 0o640)
except FileExistsError:
    metadata = os.lstat(path)
    hardened = (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == deploy_gid
        and stat.S_IMODE(metadata.st_mode) == 0o640
        and metadata.st_nlink == 1
    )
    legacy = (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and stat.S_IMODE(metadata.st_mode) == 0o644
        and metadata.st_nlink == 1
    )
    if not hardened and not legacy:
        raise SystemExit("unsafe existing Caddy transaction lock")
    descriptor = os.open(path, os.O_RDWR | cloexec | nofollow)
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != metadata.st_uid
        or opened.st_gid != metadata.st_gid
        or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(metadata.st_mode)
        or opened.st_nlink != 1
    ):
        os.close(descriptor)
        raise SystemExit("unsafe existing Caddy transaction lock")
    if legacy:
        os.fchown(descriptor, 0, deploy_gid)
        os.fchmod(descriptor, 0o640)
        os.fsync(descriptor)
    os.close(descriptor)
else:
    os.fchown(descriptor, 0, deploy_gid)
    os.fchmod(descriptor, 0o640)
    os.fsync(descriptor)
    os.close(descriptor)
"""
    deploy_gid = os.getegid()
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            bootstrap,
            str(CADDY_TRANSACTION_LOCK),
            str(deploy_gid),
        ]
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(CADDY_TRANSACTION_LOCK, flags)
    except OSError as error:
        raise DeploymentError(
            "Caddy transaction lock could not be opened safely"
        ) from error
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != deploy_gid
            or stat.S_IMODE(metadata.st_mode) != 0o640
            or metadata.st_nlink != 1
        ):
            raise DeploymentError("Caddy transaction lock metadata is unsafe")
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeploymentError(
                        "Caddy transaction lock acquisition timed out"
                    ) from None
                time.sleep(min(0.1, remaining))
            except OSError as error:
                raise DeploymentError(
                    "Caddy transaction lock could not be acquired"
                ) from error
        current = CADDY_TRANSACTION_LOCK.lstat()
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise DeploymentError("Caddy transaction lock changed while acquiring it")
        if hasattr(runner, "_caddy_mutation_fence_token"):
            raise DeploymentError("nested Caddy mutation fencing is not permitted")
        mutation_fence_token = secrets.token_hex(32)
        _activate_caddy_mutation_fence(runner, mutation_fence_token)
        runner._caddy_mutation_fence_token = mutation_fence_token
        try:
            yield
        finally:
            try:
                _deactivate_caddy_mutation_fence(runner, mutation_fence_token)
            finally:
                if (
                    hasattr(runner, "_caddy_mutation_fence_token")
                    and runner._caddy_mutation_fence_token == mutation_fence_token
                ):
                    del runner._caddy_mutation_fence_token
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _adapt_fragment(runner: CommandRunner, fragment: str) -> dict[str, Any]:
    result = runner.run(
        [CADDY_BINARY, "adapt", "--adapter", "caddyfile", "--config", "/dev/stdin"],
        input_text=fragment,
    )
    try:
        adapted = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise DeploymentError(
            "Caddy fragment adaptation returned invalid JSON"
        ) from None
    if not isinstance(adapted, dict):
        raise DeploymentError("Caddy fragment adaptation returned an invalid document")
    return cast(dict[str, Any], adapted)


def _active_caddy_config(runner: CommandRunner) -> dict[str, Any]:
    raw_config = runner.run(
        [
            CURL_BINARY,
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
    ).stdout
    try:
        document = json.loads(raw_config)
    except json.JSONDecodeError:
        raise DeploymentError("Caddy admin API returned invalid JSON") from None
    if not isinstance(document, dict):
        raise DeploymentError("Caddy admin API returned an invalid document")
    return cast(dict[str, Any], document)


def _extract_managed_caddy_route(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise DeploymentError("Caddy configuration is not a document")
    apps = document.get("apps")
    http = apps.get("http") if isinstance(apps, dict) else None
    servers = http.get("servers") if isinstance(http, dict) else None
    if not isinstance(servers, dict):
        raise DeploymentError("Caddy configuration has no HTTP server map")

    matching_routes: list[dict[str, Any]] = []
    for server in servers.values():
        if not isinstance(server, dict):
            raise DeploymentError("Caddy HTTP server entry is invalid")
        routes = server.get("routes", [])
        if not isinstance(routes, list):
            raise DeploymentError("Caddy HTTP server routes are invalid")
        for route in routes:
            if not isinstance(route, dict):
                raise DeploymentError("Caddy HTTP route is invalid")
            matchers = route.get("match")
            if not isinstance(matchers, list):
                continue
            exact_host_match = any(
                isinstance(matcher, dict)
                and set(matcher) == {"host"}
                and matcher.get("host") == [CADDY_HOST]
                for matcher in matchers
            )
            if exact_host_match:
                matching_routes.append(cast(dict[str, Any], route))
    if len(matching_routes) != 1:
        raise DeploymentError(
            "Caddy active configuration must contain exactly one managed host route"
        )
    return matching_routes[0]


def _assert_active_caddy_route(runner: CommandRunner, expected_fragment: str) -> None:
    expected_route = _extract_managed_caddy_route(
        _adapt_fragment(runner, expected_fragment)
    )
    active_route = _extract_managed_caddy_route(_active_caddy_config(runner))
    if active_route != expected_route:
        raise DeploymentError(
            "Caddy active managed route does not match the installed fragment"
        )


def _validate_managed_fragment(
    runner: CommandRunner,
    fragment: str,
    *,
    expected_upstream_port: int | None,
    expected_route_revision: str | None = None,
    allowed_profiles: frozenset[str],
    caddy_templates: Mapping[str, str] | None = None,
) -> ValidatedCaddyFragment:
    if not allowed_profiles or not allowed_profiles <= CADDY_MANAGED_PROFILES:
        raise DeploymentError("Caddy managed profile allowlist is invalid")
    matches = re.findall(
        r"(?<![A-Za-z0-9_.:-])127\.0\.0\.1:([0-9]{1,5})(?![0-9])", fragment
    )
    if len(matches) != 1:
        raise DeploymentError(
            "Caddy fragment must contain exactly one managed loopback upstream"
        )
    upstream_port = validate_candidate_port(matches[0])
    if expected_upstream_port is not None:
        validated_expected = validate_candidate_port(str(expected_upstream_port))
        if upstream_port != validated_expected:
            raise DeploymentError(
                "Caddy fragment upstream does not match the transaction"
            )

    adapted = _adapt_fragment(runner, fragment)
    revision_matches = re.findall(
        r'(?m)^\s*X-CommerceOps-Route-Revision\s+"?([0-9a-f]{64})"?\s*$',
        fragment,
    )
    if len(revision_matches) > 1:
        raise DeploymentError("Caddy fragment route revision is ambiguous")
    route_revision = revision_matches[0] if revision_matches else None
    if (
        expected_route_revision is not None
        and route_revision != expected_route_revision
    ):
        raise DeploymentError("Caddy fragment route revision does not match")
    templates = (
        {
            profile: template_path.read_text(encoding="utf-8")
            for profile, template_path in CADDY_PROFILE_TEMPLATES.items()
        }
        if caddy_templates is None
        else caddy_templates
    )
    if set(templates) != CADDY_MANAGED_PROFILES or any(
        not isinstance(template, str) for template in templates.values()
    ):
        raise DeploymentError("Caddy verified template set is invalid")
    matching_profiles: list[str] = []
    for profile, template in templates.items():
        template_has_revision = CADDY_REVISION_PLACEHOLDER in template
        if template_has_revision != (route_revision is not None):
            continue
        rendered_template = render_caddy_template(
            template,
            upstream_port,
            route_revision=route_revision,
        )
        if adapted == _adapt_fragment(runner, rendered_template):
            matching_profiles.append(profile)
    if len(matching_profiles) != 1:
        raise DeploymentError(
            "Caddy fragment is not the exact managed single-site structure"
        )
    profile = matching_profiles[0]
    if profile not in allowed_profiles:
        raise DeploymentError("Caddy fragment profile is not permitted for this action")
    return ValidatedCaddyFragment(
        profile=profile,
        upstream_port=upstream_port,
        route_revision=route_revision,
    )


def _root_path_metadata(
    runner: CommandRunner,
    path: Path,
) -> tuple[int, int, int, int]:
    result = runner.run(
        [
            SUDO_BINARY,
            "stat",
            "--format=%f|%u|%g|%h",
            "--",
            str(path),
        ]
    )
    fields = result.stdout.strip().split("|")
    if len(fields) != 4:
        raise DeploymentError("trusted Caddy transaction metadata is invalid")
    try:
        return int(fields[0], 16), int(fields[1]), int(fields[2]), int(fields[3])
    except ValueError:
        raise DeploymentError("trusted Caddy transaction metadata is invalid") from None


def _assert_root_owned_transaction_path(
    runner: CommandRunner,
    path: Path,
    *,
    directory: bool,
) -> None:
    mode, uid, gid, links = _root_path_metadata(runner, path)
    expected_mode = 0o700 if directory else 0o600
    expected_kind = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if (
        not expected_kind
        or uid != 0
        or gid != 0
        or stat.S_IMODE(mode) != expected_mode
        or (not directory and links != 1)
    ):
        kind = "directory" if directory else "file"
        raise DeploymentError(
            f"trusted Caddy transaction {kind} must be root-owned and immutable "
            f"to the deployment user"
        )


def _ensure_caddy_transaction_directory(runner: CommandRunner) -> None:
    transaction_parent = CADDY_TRANSACTION_DIRECTORY.parent
    transaction_anchor = transaction_parent.parent
    runner.run(
        [
            SUDO_BINARY,
            "install",
            "-d",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "0700",
            str(CADDY_TRANSACTION_DIRECTORY),
        ]
    )
    durability_helper = """\
import os
import stat
import sys

anchor_path, parent_path, target_path = sys.argv[1:]
if (
    not all(os.path.isabs(path) for path in (anchor_path, parent_path, target_path))
    or os.path.dirname(parent_path) != anchor_path
    or os.path.dirname(target_path) != parent_path
):
    raise SystemExit("transaction directory hierarchy is invalid")

directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
directory_flags |= getattr(os, "O_CLOEXEC", 0)
directory_flags |= getattr(os, "O_NOFOLLOW", 0)

def validate(metadata, *, exact_mode=None):
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or (mode != exact_mode if exact_mode is not None else mode & 0o022 != 0)
    ):
        raise SystemExit("unsafe transaction directory hierarchy")

anchor_stat = os.lstat(anchor_path)
validate(anchor_stat)
anchor_descriptor = os.open(anchor_path, directory_flags)
try:
    opened_anchor = os.fstat(anchor_descriptor)
    validate(opened_anchor)
    if (opened_anchor.st_dev, opened_anchor.st_ino) != (
        anchor_stat.st_dev,
        anchor_stat.st_ino,
    ):
        raise SystemExit("transaction directory anchor changed while opening")

    parent_name = os.path.basename(parent_path)
    parent_stat = os.stat(
        parent_name,
        dir_fd=anchor_descriptor,
        follow_symlinks=False,
    )
    validate(parent_stat)
    parent_descriptor = os.open(
        parent_name,
        directory_flags,
        dir_fd=anchor_descriptor,
    )
    try:
        opened_parent = os.fstat(parent_descriptor)
        validate(opened_parent)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            parent_stat.st_dev,
            parent_stat.st_ino,
        ):
            raise SystemExit("transaction directory parent changed while opening")

        target_name = os.path.basename(target_path)
        target_stat = os.stat(
            target_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        validate(target_stat, exact_mode=0o700)
        target_descriptor = os.open(
            target_name,
            directory_flags,
            dir_fd=parent_descriptor,
        )
        try:
            opened_target = os.fstat(target_descriptor)
            validate(opened_target, exact_mode=0o700)
            if (opened_target.st_dev, opened_target.st_ino) != (
                target_stat.st_dev,
                target_stat.st_ino,
            ):
                raise SystemExit("transaction directory changed while opening")
            os.fsync(target_descriptor)
            os.fsync(parent_descriptor)
            os.fsync(anchor_descriptor)
            if (
                os.fstat(target_descriptor).st_ino != target_stat.st_ino
                or os.fstat(parent_descriptor).st_ino != parent_stat.st_ino
                or os.fstat(anchor_descriptor).st_ino != anchor_stat.st_ino
            ):
                raise SystemExit("transaction directory hierarchy changed during sync")
        finally:
            os.close(target_descriptor)
    finally:
        os.close(parent_descriptor)
finally:
    os.close(anchor_descriptor)
"""
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            durability_helper,
            str(transaction_anchor),
            str(transaction_parent),
            str(CADDY_TRANSACTION_DIRECTORY),
        ]
    )
    _assert_root_owned_transaction_path(
        runner, CADDY_TRANSACTION_DIRECTORY, directory=True
    )


def _read_root_owned_transaction_text(
    runner: CommandRunner,
    path: Path,
) -> str:
    _assert_root_owned_transaction_path(
        runner, CADDY_TRANSACTION_DIRECTORY, directory=True
    )
    _assert_root_owned_transaction_path(runner, path, directory=False)
    return runner.run([SUDO_BINARY, "cat", "--", str(path)]).stdout


def _transaction_paths(transaction_id: str) -> tuple[Path, Path]:
    if TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None:
        raise DeploymentError("Caddy transaction ID is invalid")
    backup_path = CADDY_TRANSACTION_DIRECTORY / (
        f"commerce-ops-desk.{transaction_id}.conf"
    )
    return backup_path, backup_path.with_suffix(".json")


def _route_state_from_backup_payload(value: Mapping[str, object]) -> RouteState:
    return _validate_route_state_payload(
        {
            field: value[field]
            for field in (
                "fragment_sha256",
                "profile",
                "route_revision",
                "upstream",
                "deployment_assets",
            )
        }
    )


def _route_states_match(left: RouteState, right: RouteState) -> bool:
    return _route_state_payload(left) == _route_state_payload(right)


def _persisted_transactions_match(
    left: PersistedCaddyTransaction,
    right: PersistedCaddyTransaction,
) -> bool:
    return (
        left.transaction_id == right.transaction_id
        and left.operation == right.operation
        and left.bootstrap_id == right.bootstrap_id
        and left.backup_path == right.backup_path
        and left.ledger_path == right.ledger_path
        and left.ledger_text == right.ledger_text
        and left.ledger_sha256 == right.ledger_sha256
        and _route_states_match(left.backup, right.backup)
        and _route_states_match(left.installed, right.installed)
    )


def _active_chains_match(
    left: ActiveCaddyChain | None,
    right: ActiveCaddyChain | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.active_text == right.active_text
        and left.active_sha256 == right.active_sha256
        and left.bootstrap_id == right.bootstrap_id
        and left.transaction_id == right.transaction_id
        and _route_states_match(left.installed, right.installed)
        and _persisted_transactions_match(left.transaction, right.transaction)
    )


def _root_owned_regular_file_exists(
    runner: CommandRunner,
    path: Path,
) -> bool:
    symlink = runner.run(
        [SUDO_BINARY, "test", "-L", str(path)],
        allowed_returncodes=frozenset({0, 1}),
    )
    if symlink.returncode == 0:
        raise DeploymentError("trusted Caddy transaction path must not be a symlink")
    regular = runner.run(
        [SUDO_BINARY, "test", "-f", str(path)],
        allowed_returncodes=frozenset({0, 1}),
    )
    if regular.returncode == 0:
        return True
    exists = runner.run(
        [SUDO_BINARY, "test", "-e", str(path)],
        allowed_returncodes=frozenset({0, 1}),
    )
    if exists.returncode == 0:
        raise DeploymentError("trusted Caddy transaction path is not a regular file")
    return False


def _load_persisted_caddy_transaction(
    runner: CommandRunner,
    ledger_path: Path,
    *,
    validate_fragment: bool,
) -> tuple[PersistedCaddyTransaction, str]:
    if (
        not ledger_path.is_absolute()
        or ledger_path.parent != CADDY_TRANSACTION_DIRECTORY
    ):
        raise DeploymentError("Caddy transaction ledger path is invalid")
    ledger_text = _read_root_owned_transaction_text(runner, ledger_path)
    try:
        raw_record = json.loads(ledger_text)
    except json.JSONDecodeError:
        raise DeploymentError("Caddy transaction ledger is invalid") from None
    record = _validate_transaction_ledger_payload(raw_record)
    if ledger_text != _stable_json_record(record, label="Caddy transaction ledger"):
        raise DeploymentError("Caddy transaction ledger is not canonical JSON")
    transaction_id = cast(str, record["transaction_id"])
    expected_backup, expected_ledger = _transaction_paths(transaction_id)
    if ledger_path != expected_ledger:
        raise DeploymentError("Caddy transaction ledger path binding is invalid")
    raw_backup = cast(dict[str, object], record["backup"])
    backup_path = Path(cast(str, raw_backup["path"]))
    if backup_path != expected_backup:
        raise DeploymentError("Caddy transaction backup path binding is invalid")
    backup_route = _route_state_from_backup_payload(raw_backup)
    installed_route = _validate_route_state_payload(record["installed"])
    backup_fragment = _read_root_owned_transaction_text(runner, backup_path)
    if (
        hashlib.sha256(backup_fragment.encode("utf-8")).hexdigest()
        != backup_route.fragment_sha256
    ):
        raise DeploymentError("Caddy transaction backup content digest is invalid")
    if validate_fragment:
        validated_fragment = _validate_managed_fragment(
            runner,
            backup_fragment,
            expected_upstream_port=backup_route.upstream.host_port,
            expected_route_revision=backup_route.route_revision,
            allowed_profiles=frozenset({backup_route.profile}),
        )
        if (
            validated_fragment.profile != backup_route.profile
            or validated_fragment.route_revision != backup_route.route_revision
        ):
            raise DeploymentError("Caddy transaction backup route binding is invalid")
    transaction = PersistedCaddyTransaction(
        transaction_id=transaction_id,
        operation=cast(str, record["operation"]),
        bootstrap_id=cast(str, record["bootstrap_id"]),
        backup_path=backup_path,
        ledger_path=ledger_path,
        ledger_text=ledger_text,
        ledger_sha256=hashlib.sha256(ledger_text.encode("utf-8")).hexdigest(),
        backup=backup_route,
        installed=installed_route,
    )
    return transaction, backup_fragment


def _load_active_caddy_chain_with_fragment_policy(
    runner: CommandRunner,
    *,
    allow_missing: bool,
    validate_fragment: bool,
) -> ActiveCaddyChain | None:
    loaded_state = _load_active_state_record(
        runner,
        allow_missing=allow_missing,
    )
    if loaded_state is None:
        return None
    active, active_text = loaded_state
    return _active_caddy_chain_from_state(
        runner,
        active,
        active_text,
        validate_fragment=validate_fragment,
    )


def _active_caddy_chain_from_state(
    runner: CommandRunner,
    active: Mapping[str, Any],
    active_text: str,
    *,
    validate_fragment: bool,
) -> ActiveCaddyChain:
    transaction_id = cast(str, active["transaction_id"])
    ledger_path = Path(cast(str, active["ledger_path"]))
    transaction, _ = _load_persisted_caddy_transaction(
        runner,
        ledger_path,
        validate_fragment=validate_fragment,
    )
    actual_ledger_sha256 = hashlib.sha256(
        transaction.ledger_text.encode("utf-8")
    ).hexdigest()
    if active["ledger_sha256"] != actual_ledger_sha256:
        raise DeploymentError("active Caddy ledger digest binding is invalid")
    active_installed = _validate_route_state_payload(active["installed"])
    if (
        transaction.transaction_id != transaction_id
        or transaction.bootstrap_id != active["bootstrap_id"]
        or not _route_states_match(transaction.installed, active_installed)
    ):
        raise DeploymentError("active Caddy transaction binding is invalid")
    return ActiveCaddyChain(
        active_text=active_text,
        active_sha256=hashlib.sha256(active_text.encode("utf-8")).hexdigest(),
        bootstrap_id=transaction.bootstrap_id,
        transaction_id=transaction.transaction_id,
        installed=active_installed,
        transaction=transaction,
    )


def _load_active_state_record(
    runner: CommandRunner,
    *,
    allow_missing: bool,
) -> tuple[dict[str, Any], str] | None:
    if CADDY_ACTIVE_STATE.parent != CADDY_TRANSACTION_DIRECTORY:
        raise DeploymentError("active Caddy state path is invalid")
    if not _root_owned_regular_file_exists(runner, CADDY_ACTIVE_STATE):
        if allow_missing:
            return None
        raise DeploymentError("active Caddy state does not exist")
    active_text = _read_root_owned_transaction_text(runner, CADDY_ACTIVE_STATE)
    try:
        raw_active = json.loads(active_text)
    except json.JSONDecodeError:
        raise DeploymentError("active Caddy state is invalid") from None
    active = _validate_active_state_payload(raw_active)
    if active_text != _stable_json_record(active, label="active Caddy state"):
        raise DeploymentError("active Caddy state is not canonical JSON")
    transaction_id = cast(str, active["transaction_id"])
    _, expected_ledger = _transaction_paths(transaction_id)
    ledger_path = Path(cast(str, active["ledger_path"]))
    if ledger_path != expected_ledger:
        raise DeploymentError("active Caddy ledger path binding is invalid")
    return active, active_text


def _load_active_caddy_chain(
    runner: CommandRunner,
    *,
    allow_missing: bool,
) -> ActiveCaddyChain | None:
    return _load_active_caddy_chain_with_fragment_policy(
        runner,
        allow_missing=allow_missing,
        validate_fragment=True,
    )


def _install_root_owned_immutable_file(
    runner: CommandRunner,
    source: Path,
    target: Path,
) -> None:
    if (
        not target.is_absolute()
        or target.parent != CADDY_TRANSACTION_DIRECTORY
        or target == CADDY_ACTIVE_STATE
    ):
        raise DeploymentError("immutable Caddy transaction target is invalid")
    staging = CADDY_TRANSACTION_DIRECTORY / (
        f".immutable-{target.name}.{secrets.token_hex(8)}.tmp"
    )
    durability_helper = """\
import os
import stat
import sys

path = sys.argv[1]
directory = sys.argv[2]
if os.path.dirname(path) != directory:
    raise SystemExit("immutable transaction path escaped its directory")
directory_stat = os.lstat(directory)
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != 0
    or directory_stat.st_gid != 0
    or stat.S_IMODE(directory_stat.st_mode) != 0o700
):
    raise SystemExit("unsafe transaction directory")
path_stat = os.lstat(path)
if (
    not stat.S_ISREG(path_stat.st_mode)
    or path_stat.st_uid != 0
    or path_stat.st_gid != 0
    or stat.S_IMODE(path_stat.st_mode) != 0o600
    or path_stat.st_nlink != 2
):
    raise SystemExit("unsafe immutable transaction file")
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 2
    ):
        raise SystemExit("immutable transaction file changed while opening")
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory_descriptor = os.open(
    directory,
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
"""
    cleanup_durability_helper = """\
import os
import stat
import sys

path = sys.argv[1]
directory = sys.argv[2]
if os.path.dirname(path) != directory:
    raise SystemExit("immutable transaction path escaped its directory")
directory_stat = os.lstat(directory)
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != 0
    or directory_stat.st_gid != 0
    or stat.S_IMODE(directory_stat.st_mode) != 0o700
):
    raise SystemExit("unsafe transaction directory")
path_stat = os.lstat(path)
if (
    not stat.S_ISREG(path_stat.st_mode)
    or path_stat.st_uid != 0
    or path_stat.st_gid != 0
    or stat.S_IMODE(path_stat.st_mode) != 0o600
    or path_stat.st_nlink != 1
):
    raise SystemExit("unsafe immutable transaction file after staging cleanup")
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
    ):
        raise SystemExit("immutable transaction file changed after staging cleanup")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(directory, directory_flags)
    try:
        opened_directory = os.fstat(directory_descriptor)
        if (
            (opened_directory.st_dev, opened_directory.st_ino)
            != (directory_stat.st_dev, directory_stat.st_ino)
            or not stat.S_ISDIR(opened_directory.st_mode)
            or opened_directory.st_uid != 0
            or opened_directory.st_gid != 0
            or stat.S_IMODE(opened_directory.st_mode) != 0o700
        ):
            raise SystemExit("transaction directory changed while opening")
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    final = os.fstat(descriptor)
    if (
        (final.st_dev, final.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        or final.st_nlink != 1
    ):
        raise SystemExit("immutable transaction link count changed during cleanup")
finally:
    os.close(descriptor)
"""
    published = False
    try:
        runner.run(
            [
                SUDO_BINARY,
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0600",
                str(source),
                str(staging),
            ]
        )
        runner.run([SUDO_BINARY, "ln", "--", str(staging), str(target)])
        runner.run(
            [
                SUDO_BINARY,
                PYTHON_BINARY,
                "-I",
                "-c",
                durability_helper,
                str(target),
                str(CADDY_TRANSACTION_DIRECTORY),
            ]
        )
        published = True
    finally:
        try:
            cleanup = runner.run(
                [SUDO_BINARY, "rm", "-f", "--", str(staging)],
                allowed_returncodes=frozenset({0, 1}),
            )
        except DeploymentError:
            if published:
                raise
        else:
            if published:
                if cleanup.returncode != 0:
                    raise DeploymentError(
                        "immutable Caddy transaction staging cleanup failed"
                    )
                runner.run(
                    [
                        SUDO_BINARY,
                        PYTHON_BINARY,
                        "-I",
                        "-c",
                        cleanup_durability_helper,
                        str(target),
                        str(CADDY_TRANSACTION_DIRECTORY),
                    ]
                )


def _persist_caddy_transaction(
    runner: CommandRunner,
    original: str,
    *,
    operation: str,
    parent: ActiveCaddyChain | None,
    backup: RouteState,
    installed: RouteState,
) -> PersistedCaddyTransaction:
    if operation not in {"switch", "rollback"}:
        raise DeploymentError("Caddy transaction operation is invalid")
    validated_backup = _validate_route_state_payload(_route_state_payload(backup))
    validated_installed = _validate_route_state_payload(_route_state_payload(installed))
    if operation == "switch" and validated_installed.profile != CADDY_PROFILE_HARDENED:
        raise DeploymentError("Caddy transaction profile binding is invalid")
    if (
        hashlib.sha256(original.encode("utf-8")).hexdigest()
        != validated_backup.fragment_sha256
    ):
        raise DeploymentError("Caddy transaction backup digest is invalid")
    if parent is None:
        if operation != "switch":
            raise DeploymentError("Caddy rollback requires an active parent head")
        bootstrap_id = secrets.token_hex(32)
        parent_transaction_id: str | None = None
        parent_active_sha256: str | None = None
    else:
        if (
            hashlib.sha256(parent.active_text.encode("utf-8")).hexdigest()
            != parent.active_sha256
            or parent.transaction_id != parent.transaction.transaction_id
            or parent.bootstrap_id != parent.transaction.bootstrap_id
        ):
            raise DeploymentError("Caddy transaction parent head is invalid")
        if not _route_states_match(validated_backup, parent.installed):
            raise DeploymentError("Caddy transaction parent chain is not continuous")
        bootstrap_id = parent.bootstrap_id
        parent_transaction_id = parent.transaction_id
        parent_active_sha256 = parent.active_sha256
    if (
        operation == "rollback"
        and parent is not None
        and not _route_states_match(
            validated_installed,
            parent.transaction.backup,
        )
    ):
        raise DeploymentError("Caddy rollback target is not the parent backup")
    _ensure_caddy_transaction_directory(runner)
    transaction_id = (
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(16)}"
    )
    backup_path, ledger_path = _transaction_paths(transaction_id)
    record: dict[str, object] = {
        "schema": CADDY_TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "operation": operation,
        "bootstrap_id": bootstrap_id,
        "parent": {
            "transaction_id": parent_transaction_id,
            "active_sha256": parent_active_sha256,
        },
        "site": {"path": str(CADDY_SITE), "host": CADDY_HOST},
        "backup": {
            "path": str(backup_path),
            **_route_state_payload(validated_backup),
        },
        "installed": _route_state_payload(validated_installed),
    }
    _validate_transaction_ledger_payload(record)
    ledger_text = _stable_json_record(record, label="Caddy transaction ledger")
    ledger_sha256 = hashlib.sha256(ledger_text.encode("utf-8")).hexdigest()
    token = secrets.token_hex(8)
    local_backup = STATE_DIRECTORY / f".caddy-backup.{token}.conf"
    local_ledger = STATE_DIRECTORY / f".caddy-ledger.{token}.json"
    _write_private_text(local_backup, original)
    try:
        _write_private_text(local_ledger, ledger_text)
        _install_root_owned_immutable_file(
            runner,
            local_backup,
            backup_path,
        )
        _install_root_owned_immutable_file(
            runner,
            local_ledger,
            ledger_path,
        )
        if _read_root_owned_transaction_text(runner, backup_path) != original:
            raise DeploymentError("root-owned Caddy backup verification failed")
        if _read_root_owned_transaction_text(runner, ledger_path) != ledger_text:
            raise DeploymentError("root-owned Caddy ledger verification failed")
    finally:
        for temporary in (local_backup, local_ledger):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return PersistedCaddyTransaction(
        transaction_id=transaction_id,
        operation=operation,
        bootstrap_id=bootstrap_id,
        backup_path=backup_path,
        ledger_path=ledger_path,
        ledger_text=ledger_text,
        ledger_sha256=ledger_sha256,
        backup=validated_backup,
        installed=validated_installed,
    )


def _atomic_install_root_owned_text(
    runner: CommandRunner,
    path: Path,
    text: str,
    *,
    expected_sha256: str | None,
) -> None:
    if path != CADDY_ACTIVE_STATE or path.parent != CADDY_TRANSACTION_DIRECTORY:
        raise DeploymentError("active Caddy state path is invalid")
    if (
        expected_sha256 is not None
        and SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise DeploymentError("active Caddy parent digest is invalid")
    fence_token = _require_caddy_mutation_fence_token(runner)
    helper = (
        CADDY_MUTATION_FENCE_PRELUDE
        + """\

import hashlib
import os
import stat
import sys

path = sys.argv[1]
expected = None if sys.argv[2] == "-" else sys.argv[2]
directory = os.path.dirname(path)
directory_stat = os.lstat(directory)
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != 0
    or directory_stat.st_gid != 0
    or stat.S_IMODE(directory_stat.st_mode) != 0o700
):
    raise SystemExit("unsafe transaction directory")

try:
    current_stat = os.lstat(path)
except FileNotFoundError:
    current_stat = None
if expected is None:
    if current_stat is not None:
        raise SystemExit("active state already exists")
else:
    if (
        current_stat is None
        or not stat.S_ISREG(current_stat.st_mode)
        or current_stat.st_uid != 0
        or current_stat.st_gid != 0
        or stat.S_IMODE(current_stat.st_mode) != 0o600
        or current_stat.st_nlink != 1
    ):
        raise SystemExit("unsafe active state")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if (
            opened_stat.st_dev != current_stat.st_dev
            or opened_stat.st_ino != current_stat.st_ino
            or not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_uid != 0
            or opened_stat.st_gid != 0
            or stat.S_IMODE(opened_stat.st_mode) != 0o600
            or opened_stat.st_nlink != 1
        ):
            raise SystemExit("active state changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            current = source.read(1048577)
    finally:
        os.close(descriptor)
    if len(current) > 1048576:
        raise SystemExit("active state is too large")
    if hashlib.sha256(current).hexdigest() != expected:
        raise SystemExit("active state compare-and-swap failed")

content = sys.stdin.buffer.read(1048577)
if len(content) > 1048576:
    raise SystemExit("active state is too large")
temporary = os.path.join(
    directory,
    f".active.json.{os.getpid()}.{os.urandom(8).hex()}.tmp",
)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(temporary, flags, 0o600)
try:
    os.fchown(descriptor, 0, 0)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=False) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    os.close(descriptor)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
"""
    )
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            helper,
            str(path),
            "-" if expected_sha256 is None else expected_sha256,
            fence_token,
            str(CADDY_MUTATION_LOCK),
            str(CADDY_MUTATION_FENCE),
        ],
        input_text=text,
    )


def _active_chain_for_transaction(
    transaction: PersistedCaddyTransaction,
) -> ActiveCaddyChain:
    payload: dict[str, object] = {
        "schema": ACTIVE_STATE_SCHEMA,
        "bootstrap_id": transaction.bootstrap_id,
        "transaction_id": transaction.transaction_id,
        "ledger_path": str(transaction.ledger_path),
        "ledger_sha256": transaction.ledger_sha256,
        "site": {"path": str(CADDY_SITE), "host": CADDY_HOST},
        "installed": _route_state_payload(transaction.installed),
    }
    _validate_active_state_payload(payload)
    active_text = _stable_json_record(payload, label="active Caddy state")
    return ActiveCaddyChain(
        active_text=active_text,
        active_sha256=hashlib.sha256(active_text.encode("utf-8")).hexdigest(),
        bootstrap_id=transaction.bootstrap_id,
        transaction_id=transaction.transaction_id,
        installed=transaction.installed,
        transaction=transaction,
    )


def _verify_durable_root_owned_text(
    runner: CommandRunner,
    path: Path,
    expected_text: str,
) -> None:
    if path != CADDY_ACTIVE_STATE or path.parent != CADDY_TRANSACTION_DIRECTORY:
        raise DeploymentError("durable active Caddy state path is invalid")
    fence_token = _require_caddy_mutation_fence_token(runner)
    expected_sha256 = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    helper = (
        CADDY_MUTATION_FENCE_PRELUDE
        + """\

import hashlib
import os
import stat
import sys

limit = 1048576
expected_sha256 = sys.argv[1]
path = sys.argv[2]
directory = sys.argv[3]
if os.path.dirname(path) != directory:
    raise SystemExit("active state escaped its transaction directory")
expected = sys.stdin.buffer.read(limit + 1)
if len(expected) > limit or hashlib.sha256(expected).hexdigest() != expected_sha256:
    raise SystemExit("expected active state digest is invalid")

def validate_directory(metadata):
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SystemExit("unsafe transaction directory")

def validate_file(metadata):
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise SystemExit("unsafe active state")

def read_limited(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = bytearray()
    while len(content) <= limit:
        chunk = os.read(descriptor, min(65536, limit + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content)

directory_stat = os.lstat(directory)
validate_directory(directory_stat)
directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
directory_flags |= getattr(os, "O_CLOEXEC", 0)
directory_flags |= getattr(os, "O_NOFOLLOW", 0)
directory_descriptor = os.open(directory, directory_flags)
try:
    opened_directory = os.fstat(directory_descriptor)
    validate_directory(opened_directory)
    if (opened_directory.st_dev, opened_directory.st_ino) != (
        directory_stat.st_dev,
        directory_stat.st_ino,
    ):
        raise SystemExit("transaction directory changed while opening")

    path_stat = os.lstat(path)
    validate_file(path_stat)
    name = os.path.basename(path)
    relative_stat = os.stat(
        name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    validate_file(relative_stat)
    if (relative_stat.st_dev, relative_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        raise SystemExit("active state path changed before opening")
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, file_flags, dir_fd=directory_descriptor)
    try:
        opened = os.fstat(descriptor)
        validate_file(opened)
        if (opened.st_dev, opened.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise SystemExit("active state changed while opening")
        actual = read_limited(descriptor)
        if (
            actual != expected
            or hashlib.sha256(actual).hexdigest() != expected_sha256
        ):
            raise SystemExit("active state bytes do not match")
        os.fsync(descriptor)
        os.fsync(directory_descriptor)

        final_path = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        final_opened = os.fstat(descriptor)
        final_directory = os.fstat(directory_descriptor)
        validate_file(final_path)
        validate_file(final_opened)
        validate_directory(final_directory)
        if (
            (final_path.st_dev, final_path.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or (final_opened.st_dev, final_opened.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or (final_directory.st_dev, final_directory.st_ino)
            != (directory_stat.st_dev, directory_stat.st_ino)
        ):
            raise SystemExit("active state changed during durability verification")
        final = read_limited(descriptor)
        if final != expected or hashlib.sha256(final).hexdigest() != expected_sha256:
            raise SystemExit("active state bytes changed during durability verification")
    finally:
        os.close(descriptor)
finally:
    os.close(directory_descriptor)
"""
    )
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            helper,
            expected_sha256,
            str(path),
            str(CADDY_TRANSACTION_DIRECTORY),
            fence_token,
            str(CADDY_MUTATION_LOCK),
            str(CADDY_MUTATION_FENCE),
        ],
        input_text=expected_text,
    )


def _assert_persisted_transaction(
    runner: CommandRunner,
    transaction: PersistedCaddyTransaction,
    *,
    expected_parent: ActiveCaddyChain | None,
) -> None:
    if (
        transaction.ledger_path != transaction.backup_path.with_suffix(".json")
        or _transaction_paths(transaction.transaction_id)
        != (transaction.backup_path, transaction.ledger_path)
        or hashlib.sha256(transaction.ledger_text.encode("utf-8")).hexdigest()
        != transaction.ledger_sha256
    ):
        raise DeploymentError("persisted Caddy transaction identity is invalid")
    try:
        record = _validate_transaction_ledger_payload(
            json.loads(transaction.ledger_text)
        )
    except json.JSONDecodeError:
        raise DeploymentError("persisted Caddy transaction ledger is invalid") from None
    parent_record = cast(dict[str, object], record["parent"])
    expected_parent_id = (
        None if expected_parent is None else expected_parent.transaction_id
    )
    expected_parent_sha = (
        None if expected_parent is None else expected_parent.active_sha256
    )
    backup_record = cast(dict[str, object], record["backup"])
    if (
        record["transaction_id"] != transaction.transaction_id
        or record["operation"] != transaction.operation
        or record["bootstrap_id"] != transaction.bootstrap_id
        or parent_record
        != {
            "transaction_id": expected_parent_id,
            "active_sha256": expected_parent_sha,
        }
        or backup_record["path"] != str(transaction.backup_path)
        or not _route_states_match(
            _route_state_from_backup_payload(backup_record),
            transaction.backup,
        )
        or not _route_states_match(
            _validate_route_state_payload(record["installed"]),
            transaction.installed,
        )
    ):
        raise DeploymentError("persisted Caddy transaction binding is invalid")
    if (
        expected_parent is not None
        and expected_parent.bootstrap_id != transaction.bootstrap_id
    ):
        raise DeploymentError(
            "persisted Caddy transaction bootstrap binding is invalid"
        )
    if expected_parent is not None and not _route_states_match(
        transaction.backup,
        expected_parent.installed,
    ):
        raise DeploymentError(
            "persisted Caddy transaction parent chain is not continuous"
        )
    if (
        transaction.operation == "rollback"
        and expected_parent is not None
        and not _route_states_match(
            transaction.installed,
            expected_parent.transaction.backup,
        )
    ):
        raise DeploymentError(
            "persisted Caddy rollback target is not the parent backup"
        )
    if (
        _read_root_owned_transaction_text(runner, transaction.ledger_path)
        != transaction.ledger_text
    ):
        raise DeploymentError("persisted Caddy transaction ledger changed")
    backup_fragment = _read_root_owned_transaction_text(runner, transaction.backup_path)
    if (
        hashlib.sha256(backup_fragment.encode("utf-8")).hexdigest()
        != transaction.backup.fragment_sha256
    ):
        raise DeploymentError("persisted Caddy transaction backup changed")


def _commit_active_caddy_state(
    runner: CommandRunner,
    transaction: PersistedCaddyTransaction,
    *,
    expected_parent: ActiveCaddyChain | None,
) -> ActiveCaddyChain:
    _assert_persisted_transaction(
        runner,
        transaction,
        expected_parent=expected_parent,
    )
    proposed = _active_chain_for_transaction(transaction)
    try:
        _atomic_install_root_owned_text(
            runner,
            CADDY_ACTIVE_STATE,
            proposed.active_text,
            expected_sha256=(
                None if expected_parent is None else expected_parent.active_sha256
            ),
        )
    except Exception as commit_error:
        commit_timed_out = _exception_contains_timeout(commit_error)
        try:
            observed = _load_active_caddy_chain_with_fragment_policy(
                runner,
                allow_missing=True,
                validate_fragment=False,
            )
        except Exception as observation_error:
            error_type = (
                IndeterminateCaddyMutationError if commit_timed_out else DeploymentError
            )
            raise error_type(
                "active Caddy head commit is indeterminate after verification failed"
            ) from observation_error
        if _active_chains_match(observed, proposed):
            try:
                _verify_durable_root_owned_text(
                    runner,
                    CADDY_ACTIVE_STATE,
                    proposed.active_text,
                )
                durable_observed = _load_active_caddy_chain_with_fragment_policy(
                    runner,
                    allow_missing=True,
                    validate_fragment=False,
                )
            except Exception as durability_error:
                error_type = (
                    IndeterminateCaddyMutationError
                    if commit_timed_out
                    else DeploymentError
                )
                raise error_type(
                    "active Caddy head is visible but commit durability is "
                    "indeterminate"
                ) from durability_error
            if not _active_chains_match(durable_observed, proposed):
                error_type = (
                    IndeterminateCaddyMutationError
                    if commit_timed_out
                    else DeploymentError
                )
                raise error_type(
                    "active Caddy head changed during durability verification and "
                    "is indeterminate"
                ) from commit_error
            return proposed
        if _active_chains_match(observed, expected_parent):
            if commit_timed_out:
                raise IndeterminateCaddyMutationError(
                    "active Caddy head commit timed out and remains indeterminate"
                ) from commit_error
            if isinstance(commit_error, DeploymentError):
                raise
            raise DeploymentError(
                "active Caddy head was not committed"
            ) from commit_error
        error_type = (
            IndeterminateCaddyMutationError if commit_timed_out else DeploymentError
        )
        raise error_type(
            "active Caddy head commit encountered external drift and is indeterminate"
        ) from commit_error
    return proposed


def _atomic_install_site(
    runner: CommandRunner,
    content: str,
    *,
    expected_sha256: str,
    expected_current_sha256: str,
) -> None:
    if (
        SHA256_PATTERN.fullmatch(expected_sha256) is None
        or SHA256_PATTERN.fullmatch(expected_current_sha256) is None
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_sha256
    ):
        raise DeploymentError("Caddy site content digest is invalid")
    fence_token = _require_caddy_mutation_fence_token(runner)
    helper = (
        CADDY_MUTATION_FENCE_PRELUDE
        + """\

import hashlib
import os
import stat
import sys

path = sys.argv[1]
expected = sys.argv[2]
expected_current = sys.argv[3]
directory = os.path.dirname(path)
directory_stat = os.lstat(directory)
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != 0
    or directory_stat.st_gid != 0
    or stat.S_IMODE(directory_stat.st_mode) & 0o022
):
    raise SystemExit("unsafe Caddy sites directory")
site_stat = os.lstat(path)
if (
    not stat.S_ISREG(site_stat.st_mode)
    or site_stat.st_uid != 0
    or site_stat.st_gid != 0
    or stat.S_IMODE(site_stat.st_mode) != 0o644
    or site_stat.st_nlink != 1
):
    raise SystemExit("unsafe Caddy site metadata")
current_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
current_flags |= getattr(os, "O_NOFOLLOW", 0)
current_descriptor = os.open(path, current_flags)
try:
    opened_site = os.fstat(current_descriptor)
    if (
        (opened_site.st_dev, opened_site.st_ino)
        != (site_stat.st_dev, site_stat.st_ino)
        or not stat.S_ISREG(opened_site.st_mode)
        or opened_site.st_uid != 0
        or opened_site.st_gid != 0
        or stat.S_IMODE(opened_site.st_mode) != 0o644
        or opened_site.st_nlink != 1
    ):
        raise SystemExit("Caddy site changed while opening")
    current = os.read(current_descriptor, 1048577)
finally:
    os.close(current_descriptor)
if len(current) > 1048576 or hashlib.sha256(current).hexdigest() != expected_current:
    raise SystemExit("current Caddy site digest changed")
content = sys.stdin.buffer.read(1048577)
if len(content) > 1048576:
    raise SystemExit("Caddy site content is too large")
if hashlib.sha256(content).hexdigest() != expected:
    raise SystemExit("Caddy site content digest changed")
temporary = os.path.join(
    directory,
    f".{os.path.basename(path)}.{os.getpid()}.{os.urandom(8).hex()}.tmp",
)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(temporary, flags, 0o644)
try:
    os.fchown(descriptor, 0, 0)
    os.fchmod(descriptor, 0o644)
    with os.fdopen(descriptor, "wb", closefd=False) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    os.close(descriptor)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
"""
    )
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            helper,
            str(CADDY_SITE),
            expected_sha256,
            expected_current_sha256,
            fence_token,
            str(CADDY_MUTATION_LOCK),
            str(CADDY_MUTATION_FENCE),
        ],
        input_text=content,
    )


def _validate_caddy(runner: CommandRunner) -> None:
    runner.run(
        [
            SUDO_BINARY,
            CADDY_BINARY,
            "validate",
            "--adapter",
            "caddyfile",
            "--config",
            str(CADDY_MAIN_CONFIG),
        ]
    )


def _reload_caddy(runner: CommandRunner) -> None:
    fence_token = _require_caddy_mutation_fence_token(runner)
    helper = (
        CADDY_MUTATION_FENCE_PRELUDE
        + f"""\

import subprocess

result = subprocess.run(
    [{SYSTEMCTL_BINARY!r}, "reload", "caddy"],
    env={dict(SAFE_PRIVILEGED_ENVIRONMENT)!r},
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
if result.returncode != 0:
    detail = (result.stderr.strip() or result.stdout.strip())[:2000]
    if detail:
        sys.stderr.write(detail + "\\n")
    raise SystemExit(result.returncode)
"""
    )
    runner.run(
        [
            SUDO_BINARY,
            PYTHON_BINARY,
            "-I",
            "-c",
            helper,
            fence_token,
            str(CADDY_MUTATION_LOCK),
            str(CADDY_MUTATION_FENCE),
        ],
        timeout=None,
    )


def _validate_and_reload_caddy(runner: CommandRunner) -> None:
    """Compatibility wrapper for callers that need the two fixed phases."""

    _validate_caddy(runner)
    _reload_caddy(runner)


def _smoke_caddy(expected_route: RouteState) -> None:
    request = urllib.request.Request(
        "http://127.0.0.1/ready",
        headers={"Accept": "application/json", "Host": CADDY_HOST},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            body = response.read(4_097)
            status_code = response.status
            revision_values = response.headers.get_all(CADDY_REVISION_HEADER)
    except (OSError, urllib.error.URLError) as error:
        raise DeploymentError("Caddy loopback readiness probe failed") from error
    if status_code != 200 or len(body) > 4_096:
        raise DeploymentError("Caddy loopback readiness response is invalid")
    if expected_route.profile == CADDY_PROFILE_HARDENED:
        if expected_route.route_revision is None or revision_values != [
            expected_route.route_revision
        ]:
            raise DeploymentError("Caddy loopback readiness route revision is invalid")
    elif expected_route.profile == CADDY_PROFILE_LEGACY_V020:
        if revision_values is not None:
            raise DeploymentError(
                "legacy Caddy route unexpectedly returned a revision header"
            )
    else:
        raise DeploymentError("Caddy smoke route profile is invalid")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise DeploymentError("Caddy loopback readiness response is not JSON") from None
    if payload != {"status": "ready", "database": "reachable"}:
        raise DeploymentError("Caddy loopback readiness contract changed")


def _safe_fragment_name(label: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "-", label)
    if not sanitized or sanitized in {".", ".."}:
        raise DeploymentError("Caddy deployment label is invalid")
    return sanitized


def _load_exact_legacy_bootstrap_route(
    runner: CommandRunner,
    *,
    caddy_templates: Mapping[str, str],
) -> RouteState:
    fragment = _read_current_site(runner)
    fragment_sha256 = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    if fragment_sha256 != LEGACY_CADDY_FRAGMENT_SHA256:
        raise DeploymentError("legacy Caddy fragment is not the frozen bootstrap input")
    validated = _validate_managed_fragment(
        runner,
        fragment,
        expected_upstream_port=LEGACY_CADDY_PORT,
        expected_route_revision=None,
        allowed_profiles=frozenset({CADDY_PROFILE_LEGACY_V020}),
        caddy_templates=caddy_templates,
    )
    if (
        validated.profile != CADDY_PROFILE_LEGACY_V020
        or validated.upstream_port != LEGACY_CADDY_PORT
        or validated.route_revision is not None
    ):
        raise DeploymentError("legacy Caddy fragment bootstrap identity is invalid")
    _assert_active_caddy_route(runner, fragment)
    upstream = _upstream_identity_for_host_port(runner, LEGACY_CADDY_PORT)
    if _upstream_identity_fingerprint(upstream) != LEGACY_UPSTREAM_FINGERPRINT:
        raise DeploymentError(
            "legacy upstream does not match the frozen bootstrap identity"
        )
    route = RouteState(
        fragment_sha256=fragment_sha256,
        profile=CADDY_PROFILE_LEGACY_V020,
        route_revision=None,
        upstream=upstream,
        deployment_assets=None,
    )
    _smoke_caddy(route)
    _assert_upstream_ready(runner, upstream)
    return route


def _install_caddy_fragment(
    runner: CommandRunner,
    fragment: str,
    *,
    label: str,
    operation: str,
    current_route: RouteState,
    installed_route: RouteState,
    parent: ActiveCaddyChain | None,
    caddy_templates: Mapping[str, str] | None = None,
) -> Path:
    if operation == "switch":
        allowed_installed_profiles = frozenset({CADDY_PROFILE_HARDENED})
    elif operation == "rollback":
        allowed_installed_profiles = CADDY_MANAGED_PROFILES
    else:
        raise DeploymentError("Caddy transaction operation is invalid")
    _validate_route_state_payload(_route_state_payload(current_route))
    _validate_route_state_payload(_route_state_payload(installed_route))
    if parent is not None and not _route_states_match(parent.installed, current_route):
        raise DeploymentError("current Caddy route does not match the active head")
    if installed_route.profile not in allowed_installed_profiles:
        raise DeploymentError("Caddy installed route profile is not permitted")
    installed_fragment = _validate_managed_fragment(
        runner,
        fragment,
        expected_upstream_port=installed_route.upstream.host_port,
        expected_route_revision=installed_route.route_revision,
        allowed_profiles=allowed_installed_profiles,
        caddy_templates=caddy_templates,
    )
    installed_digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    if (
        installed_digest != installed_route.fragment_sha256
        or installed_fragment.profile != installed_route.profile
        or installed_fragment.route_revision != installed_route.route_revision
    ):
        raise DeploymentError(
            "Caddy installed fragment is not bound to its route state"
        )
    original = _read_current_site(runner)
    original_fragment = _validate_managed_fragment(
        runner,
        original,
        expected_upstream_port=current_route.upstream.host_port,
        expected_route_revision=current_route.route_revision,
        allowed_profiles=frozenset({current_route.profile}),
        caddy_templates=caddy_templates,
    )
    original_digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if (
        original_digest != current_route.fragment_sha256
        or original_fragment.profile != current_route.profile
        or original_fragment.route_revision != current_route.route_revision
    ):
        raise DeploymentError("current Caddy fragment is not bound to its route state")
    _assert_upstream_ready(runner, installed_route.upstream)
    _safe_fragment_name(label)
    transaction = _persist_caddy_transaction(
        runner,
        original,
        operation=operation,
        parent=parent,
        backup=current_route,
        installed=installed_route,
    )
    print(
        f"Caddy transaction prepared; reconcile ledger: {transaction.ledger_path}",
        flush=True,
    )
    install_attempted = False
    commit_attempted = False
    try:
        install_attempted = True
        _atomic_install_site(
            runner,
            fragment,
            expected_sha256=installed_digest,
            expected_current_sha256=original_digest,
        )
        installed_site = _read_current_site(runner)
        if installed_site != fragment:
            raise DeploymentError(
                "installed Caddy site bytes changed before validation"
            )
        _validate_caddy(runner)
        _reload_caddy(runner)
        _assert_active_caddy_route(runner, fragment)
        _smoke_caddy(installed_route)
        _assert_upstream_ready(runner, installed_route.upstream)
        commit_attempted = True
        _commit_active_caddy_state(
            runner,
            transaction,
            expected_parent=parent,
        )
    except Exception as deployment_error:
        if isinstance(deployment_error, IndeterminateCaddyMutationError):
            raise DeploymentError(
                "Caddy transaction mutation is indeterminate; automatic restoration "
                f"refused; reconcile ledger: {transaction.ledger_path}"
            ) from deployment_error
        if _exception_contains_timeout(deployment_error):
            raise DeploymentError(
                "Caddy transaction timed out and may still be running; automatic "
                "restoration refused; reconcile ledger: "
                f"{transaction.ledger_path}"
            ) from deployment_error
        if install_attempted:
            try:
                observed_head = _load_active_caddy_chain(
                    runner,
                    allow_missing=True,
                )
            except Exception as observation_error:
                raise DeploymentError(
                    "Caddy head is indeterminate; automatic restoration refused; "
                    f"trusted backup: {transaction.backup_path}"
                ) from observation_error
            if (
                commit_attempted
                and observed_head is not None
                and observed_head.transaction_id == transaction.transaction_id
                and observed_head.bootstrap_id == transaction.bootstrap_id
                and _route_states_match(
                    observed_head.installed,
                    transaction.installed,
                )
                and _persisted_transactions_match(
                    observed_head.transaction,
                    transaction,
                )
            ):
                raise DeploymentError(
                    "new Caddy head is visible but commit durability is "
                    "indeterminate; automatic restoration refused; trusted backup: "
                    f"{transaction.backup_path}"
                ) from deployment_error
            if not _active_chains_match(observed_head, parent):
                raise DeploymentError(
                    "Caddy active head changed outside this transaction; automatic "
                    f"restoration refused; trusted backup: {transaction.backup_path}"
                ) from deployment_error
            try:
                current = _read_current_site(runner)
            except Exception as inspection_error:
                raise DeploymentError(
                    "Caddy switch failed and automatic restoration also failed; "
                    "current site could not be verified; trusted backup: "
                    f"{transaction.backup_path}"
                ) from inspection_error
            current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if current_digest == original_digest:
                if isinstance(deployment_error, DeploymentError):
                    raise
                raise DeploymentError(
                    "Caddy switch failed before changing the managed site"
                ) from deployment_error
            if current_digest != installed_digest:
                raise DeploymentError(
                    "Caddy site changed outside this transaction; automatic "
                    f"restoration refused; trusted backup: {transaction.backup_path}"
                ) from deployment_error
            # This reread catches already-stale state. The advisory lock is not
            # an atomic CAS against a privileged writer that ignores the lock.
            try:
                _assert_upstream_ready(runner, current_route.upstream)
                _atomic_install_site(
                    runner,
                    original,
                    expected_sha256=original_digest,
                    expected_current_sha256=installed_digest,
                )
                _validate_caddy(runner)
                _reload_caddy(runner)
                _assert_active_caddy_route(runner, original)
                _smoke_caddy(current_route)
                _assert_upstream_ready(runner, current_route.upstream)
            except Exception as restoration_error:
                raise DeploymentError(
                    "Caddy switch failed and automatic restoration also failed; "
                    f"trusted backup: {transaction.backup_path}; "
                    f"managed site: {CADDY_SITE}"
                ) from restoration_error
        if isinstance(deployment_error, DeploymentError):
            raise
        raise DeploymentError(
            "Caddy switch failed before verification"
        ) from deployment_error
    return transaction.backup_path


def _validate_caddy_route_fragment(
    runner: CommandRunner,
    fragment: str,
    route: RouteState,
) -> RouteState:
    validated_route = _validate_route_state_payload(_route_state_payload(route))
    digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    if digest != validated_route.fragment_sha256:
        raise DeploymentError("reconcile site digest does not match its route state")
    validated_fragment = _validate_managed_fragment(
        runner,
        fragment,
        expected_upstream_port=validated_route.upstream.host_port,
        expected_route_revision=validated_route.route_revision,
        allowed_profiles=frozenset({validated_route.profile}),
    )
    if (
        validated_fragment.profile != validated_route.profile
        or validated_fragment.route_revision != validated_route.route_revision
    ):
        raise DeploymentError("reconcile site is not bound to its route state")
    return validated_route


def _verify_loaded_caddy_route(
    runner: CommandRunner,
    fragment: str,
    route: RouteState,
) -> None:
    _validate_caddy(runner)
    _reload_caddy(runner)
    _assert_active_caddy_route(runner, fragment)
    _smoke_caddy(route)
    _assert_upstream_ready(runner, route.upstream)


def _reverify_caddy_route(
    runner: CommandRunner,
    fragment: str,
    route: RouteState,
) -> None:
    validated_route = _validate_caddy_route_fragment(runner, fragment, route)
    _assert_upstream_ready(runner, validated_route.upstream)
    _verify_loaded_caddy_route(runner, fragment, route)


def _restore_reconcile_backup(
    runner: CommandRunner,
    transaction: PersistedCaddyTransaction,
    backup_fragment: str,
    *,
    expected_head: ActiveCaddyChain | None,
) -> None:
    observed_head = _load_active_caddy_chain(runner, allow_missing=True)
    if not _active_chains_match(observed_head, expected_head):
        raise DeploymentError(
            "Caddy active head changed; reconcile backup restoration refused"
        )
    current_fragment = _read_current_site(runner)
    current_digest = hashlib.sha256(current_fragment.encode("utf-8")).hexdigest()
    if current_digest != transaction.installed.fragment_sha256:
        raise DeploymentError(
            "Caddy site changed; reconcile backup restoration refused"
        )
    validated_backup = _validate_caddy_route_fragment(
        runner,
        backup_fragment,
        transaction.backup,
    )
    _assert_upstream_ready(runner, validated_backup.upstream)
    _atomic_install_site(
        runner,
        backup_fragment,
        expected_sha256=validated_backup.fragment_sha256,
        expected_current_sha256=current_digest,
    )
    if _read_current_site(runner) != backup_fragment:
        raise DeploymentError(
            "reconcile backup bytes changed before route verification"
        )
    _verify_loaded_caddy_route(
        runner,
        backup_fragment,
        transaction.backup,
    )


def reconcile_transaction(
    arguments: argparse.Namespace,
    runner: CommandRunner,
) -> Path:
    """Resolve one interrupted site/head transaction without guessing state."""

    with _caddy_transaction_lock(runner):
        transaction, backup_fragment = _load_persisted_caddy_transaction(
            runner,
            Path(arguments.transaction),
            validate_fragment=True,
        )
        active = _load_active_caddy_chain(runner, allow_missing=True)
        proposed = _active_chain_for_transaction(transaction)
        already_committed = _active_chains_match(active, proposed)
        if not already_committed:
            _assert_persisted_transaction(
                runner,
                transaction,
                expected_parent=active,
            )

        current_fragment = _read_current_site(runner)
        current_digest = hashlib.sha256(current_fragment.encode("utf-8")).hexdigest()
        if current_digest == transaction.installed.fragment_sha256:
            try:
                _reverify_caddy_route(
                    runner,
                    current_fragment,
                    transaction.installed,
                )
            except Exception as verification_error:
                if isinstance(
                    verification_error,
                    IndeterminateCaddyMutationError,
                ) or _exception_contains_timeout(verification_error):
                    raise DeploymentError(
                        "Caddy reconcile verification is indeterminate; automatic "
                        "backup restoration refused; reconcile ledger: "
                        f"{transaction.ledger_path}"
                    ) from verification_error
                if already_committed:
                    raise
                try:
                    _restore_reconcile_backup(
                        runner,
                        transaction,
                        backup_fragment,
                        expected_head=active,
                    )
                except Exception as restoration_error:
                    raise DeploymentError(
                        "Caddy reconcile could not verify the installed route and "
                        "trusted backup restoration also failed"
                    ) from restoration_error
                return transaction.backup_path
            if not already_committed:
                _commit_active_caddy_state(
                    runner,
                    transaction,
                    expected_parent=active,
                )
            return transaction.backup_path

        if already_committed:
            raise DeploymentError(
                "reconcile site does not match the committed transaction head"
            )
        if (
            current_digest == transaction.backup.fragment_sha256
            and current_fragment == backup_fragment
        ):
            _reverify_caddy_route(
                runner,
                current_fragment,
                transaction.backup,
            )
            return transaction.backup_path
        raise DeploymentError(
            "reconcile site matches neither the transaction backup nor installed state"
        )


def switch_candidate(arguments: argparse.Namespace, runner: CommandRunner) -> Path:
    with _caddy_transaction_lock(runner):
        state = _load_candidate_state(Path(arguments.state))
        verified_candidate = _assert_state_candidate_ready(runner, state)
        deployment_assets = verified_candidate.deployment_assets
        parent = _load_active_caddy_chain(runner, allow_missing=True)
        current_route = (
            _load_exact_legacy_bootstrap_route(
                runner,
                caddy_templates=deployment_assets.caddy_templates,
            )
            if parent is None
            else parent.installed
        )
        port = validate_candidate_port(str(state["candidate_port"]))
        template = deployment_assets.caddy_templates[CADDY_PROFILE_HARDENED]
        route_revision = _route_revision(
            CADDY_PROFILE_HARDENED,
            verified_candidate.upstream,
            deployment_assets.identity,
        )
        rendered = render_caddy_template(
            template,
            port,
            route_revision=route_revision,
        )
        installed_route = RouteState(
            fragment_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            profile=CADDY_PROFILE_HARDENED,
            route_revision=route_revision,
            upstream=verified_candidate.upstream,
            deployment_assets=deployment_assets.identity,
        )
        return _install_caddy_fragment(
            runner,
            rendered,
            label=f"before-{cast(str, state['source_sha'])[:12]}",
            operation="switch",
            current_route=current_route,
            installed_route=installed_route,
            parent=parent,
            caddy_templates=deployment_assets.caddy_templates,
        )


def _load_validated_backup(
    runner: CommandRunner,
    path: Path,
) -> TrustedCaddyBackup:
    if not path.is_absolute() or path.parent != CADDY_TRANSACTION_DIRECTORY:
        raise DeploymentError(
            "rollback backup must be directly inside the trusted transaction directory"
        )
    match = BACKUP_NAME_PATTERN.fullmatch(path.name)
    if match is None:
        raise DeploymentError("rollback backup must use a tool-generated backup name")
    loaded_state = _load_active_state_record(runner, allow_missing=False)
    if loaded_state is None:  # pragma: no cover - excluded by allow_missing=False.
        raise DeploymentError("active Caddy state does not exist")
    active, active_text = loaded_state
    active_transaction_id = cast(str, active["transaction_id"])
    expected_backup, _ = _transaction_paths(active_transaction_id)
    if (
        match.group("transaction_id") != active_transaction_id
        or path != expected_backup
    ):
        raise DeploymentError("rollback backup is not authorized by the current head")
    chain = _active_caddy_chain_from_state(
        runner,
        active,
        active_text,
        validate_fragment=True,
    )
    if path != chain.transaction.backup_path:
        raise DeploymentError("rollback backup is not authorized by the current head")
    fragment = _read_root_owned_transaction_text(runner, path)
    route = chain.transaction.backup
    if hashlib.sha256(fragment.encode("utf-8")).hexdigest() != route.fragment_sha256:
        raise DeploymentError("rollback backup content digest is invalid")
    return TrustedCaddyBackup(
        path,
        fragment,
        route.upstream.host_port,
        route.profile,
        chain,
    )


def rollback_site(arguments: argparse.Namespace, runner: CommandRunner) -> Path:
    with _caddy_transaction_lock(runner):
        backup = _load_validated_backup(runner, Path(arguments.backup))
        return _install_caddy_fragment(
            runner,
            backup.fragment,
            label=f"rollback-safety-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            operation="rollback",
            current_route=backup.parent.installed,
            installed_route=backup.parent.transaction.backup,
            parent=backup.parent,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, switch, reconcile, or roll back the CommerceOps "
            "workstation candidate."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="Create and verify an isolated candidate without changing Caddy.",
    )
    prepare.add_argument("--build-manifest", required=True)
    prepare.add_argument("--candidate-port", required=True)
    prepare.add_argument("--live-data-dir", required=True)
    prepare.add_argument("--candidate-data-dir", required=True)

    switch = subparsers.add_parser(
        "switch",
        help="Atomically point only the CommerceOps Caddy site at a healthy candidate.",
    )
    switch.add_argument("--state", required=True)

    rollback = subparsers.add_parser(
        "rollback",
        help="Restore one CommerceOps Caddy backup with validation and reload.",
    )
    rollback.add_argument("--backup", required=True)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Reconcile one interrupted site/head transaction after a fatal stop.",
    )
    reconcile.add_argument("--transaction", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    runner = CommandRunner()
    try:
        if arguments.command == "prepare":
            state = prepare_candidate(arguments, runner)
            print(f"candidate healthy; state recorded at {state}")
        elif arguments.command == "switch":
            backup = switch_candidate(arguments, runner)
            print(f"Caddy switched and verified; rollback backup: {backup}")
        elif arguments.command == "rollback":
            safety_backup = rollback_site(arguments, runner)
            print(
                f"Caddy rollback verified; replaced-site safety backup: {safety_backup}"
            )
        elif arguments.command == "reconcile":
            backup = reconcile_transaction(arguments, runner)
            print(f"Caddy transaction reconciled; trusted backup: {backup}")
        else:  # pragma: no cover - argparse enforces the command set.
            raise DeploymentError("unsupported command")
    except DeploymentError as error:
        print(f"deployment refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
