"""Runtime isolation contracts for workstation deployment candidates."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PRODUCT_ROOT / "deploy" / "workstation" / "compose.yaml"
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE_ID = "sha256:" + "1" * 64
DATA_DIRECTORY = Path("/home/deploy/apps/commerce-ops-desk/data-candidate-0123456789ab")
NETWORK_NAME = "commerce-ops-candidate-0123456789ab_default"
TRUSTED_PROXY_JSON = '["192.0.2.1/32"]'


def valid_image_config() -> dict[str, object]:
    return {
        "Cmd": ["bash", "scripts/start-hosted.sh"],
        "Env": [
            "COMMERCE_OPS_DATABASE_URL=sqlite+pysqlite:////app/data/commerce_ops.db",
            "COMMERCE_OPS_ENVIRONMENT=demo",
            "COMMERCE_OPS_VENV_DIR=/opt/venv",
            "PORT=8000",
            "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
        ],
        "ExposedPorts": {"8000/tcp": {}},
        "Healthcheck": {
            "Test": ["CMD", "/opt/venv/bin/python", "-c", "health probe"],
        },
        "User": "10001:10001",
        "Volumes": {"/app/data": {}},
        "WorkingDir": "/app",
    }


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_runtime_contract", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_inspection(module: ModuleType) -> dict[str, object]:
    identity = module.candidate_identity(VALID_SHA)
    return {
        "Image": IMAGE_ID,
        "Config": {
            "Image": identity.image,
            "User": "10001:10001",
            "Cmd": ["bash", "scripts/start-hosted.sh"],
            "Entrypoint": None,
            "ExposedPorts": {"8000/tcp": {}},
            "Healthcheck": {
                "Test": ["CMD", "/opt/venv/bin/python", "-c", "health probe"],
            },
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
                "COMMERCE_OPS_VENV_DIR=/opt/venv",
                'COMMERCE_OPS_ALLOWED_HOSTS=["commerce-ops-desk.srrsh.aig.rest","127.0.0.1","localhost"]',
                f"COMMERCE_OPS_TRUSTED_PROXY_CIDRS={TRUSTED_PROXY_JSON}",
                "COMMERCE_OPS_COOKIE_SECURE=true",
                "COMMERCE_OPS_WEBHOOK_ENABLED=true",
                "PORT=8000",
                "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
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
            "NetworkMode": NETWORK_NAME,
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
            "MaskedPaths": [
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
            ],
            "ReadonlyPaths": [
                "/proc/bus",
                "/proc/fs",
                "/proc/irq",
                "/proc/sys",
                "/proc/sysrq-trigger",
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(DATA_DIRECTORY),
                "Destination": "/app/data",
                "Mode": "",
                "Propagation": "rprivate",
                "RW": True,
            }
        ],
        "NetworkSettings": {"Networks": {NETWORK_NAME: {"Gateway": "192.0.2.1"}}},
    }


def validate(
    module: ModuleType,
    inspection: object,
    *,
    image_config: dict[str, object] | None = None,
) -> None:
    module.validate_candidate_runtime_inspection(
        inspection,
        identity=module.candidate_identity(VALID_SHA),
        expected_image_id=IMAGE_ID,
        network_name=NETWORK_NAME,
        port=18088,
        data_directory=DATA_DIRECTORY,
        trusted_proxy_json=TRUSTED_PROXY_JSON,
        image_config=valid_image_config() if image_config is None else image_config,
    )


def test_compose_pins_the_numeric_runtime_uid_and_gid() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert '    user: "10001:10001"' in compose


def test_candidate_runtime_accepts_the_exact_hardened_contract() -> None:
    module = load_deploy_tool()

    validate(module, valid_inspection(module))


def test_candidate_runtime_accepts_docker_null_devices_without_device_mapping() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config["Devices"] = None

    validate(module, inspection)


def test_candidate_runtime_accepts_omitted_image_entrypoint_when_container_is_null() -> (
    None
):
    module = load_deploy_tool()
    image_config = valid_image_config()
    assert "Entrypoint" not in image_config

    validate(module, valid_inspection(module), image_config=image_config)


@pytest.mark.parametrize(
    "security_opt",
    [["no-new-privileges:true"], ["no-new-privileges"]],
)
def test_candidate_runtime_accepts_docker_no_new_privileges_inspect_formats(
    security_opt: list[str],
) -> None:
    """Docker versions retain either the explicit true suffix or the bare option."""
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config["SecurityOpt"] = security_opt

    validate(module, inspection)


def test_candidate_runtime_accepts_docker_normalized_tmpfs_size() -> None:
    """Docker may report Compose's 64m tmpfs size as its byte count."""
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config["Tmpfs"] = {
        "/tmp": ("nodev,gid=10001,size=67108864,nosuid,rw,mode=1777,noexec,uid=10001")
    }

    validate(module, inspection)


def test_candidate_runtime_rejects_extra_docker_socket_mount() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    mounts = inspection["Mounts"]
    assert isinstance(mounts, list)
    mounts.append(
        {
            "Type": "bind",
            "Source": "/var/run/docker.sock",
            "Destination": "/var/run/docker.sock",
            "RW": True,
        }
    )

    with pytest.raises(module.DeploymentError, match="mount"):
        validate(module, inspection)


def test_candidate_runtime_rejects_extra_public_port_mapping() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    port_bindings = host_config["PortBindings"]
    assert isinstance(port_bindings, dict)
    port_bindings["9000/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "19000"}]

    with pytest.raises(module.DeploymentError, match="port"):
        validate(module, inspection)


@pytest.mark.parametrize("runtime_user", ["", "0", "0:0", "root", "10001"])
def test_candidate_runtime_rejects_root_or_incomplete_user_identity(
    runtime_user: str,
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    config = inspection["Config"]
    assert isinstance(config, dict)
    config["User"] = runtime_user

    with pytest.raises(module.DeploymentError, match="user"):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMMERCE_OPS_DATABASE_URL", "sqlite+pysqlite:////tmp/attacker.db"),
        ("COMMERCE_OPS_ENVIRONMENT", "development"),
        ("COMMERCE_OPS_ALLOWED_HOSTS", '["*"]'),
        ("COMMERCE_OPS_COOKIE_SECURE", "false"),
        ("COMMERCE_OPS_WEBHOOK_ENABLED", "false"),
        ("PORT", "9000"),
    ],
)
def test_candidate_runtime_rejects_changed_critical_environment(
    name: str, value: str
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    config = inspection["Config"]
    assert isinstance(config, dict)
    environment = config["Env"]
    assert isinstance(environment, list)
    environment[:] = [
        f"{name}={value}" if entry.startswith(f"{name}=") else entry
        for entry in environment
    ]

    with pytest.raises(module.DeploymentError, match="environment"):
        validate(module, inspection)


def test_candidate_runtime_rejects_unexpected_commerce_environment_override() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    config = inspection["Config"]
    assert isinstance(config, dict)
    environment = config["Env"]
    assert isinstance(environment, list)
    environment.append("COMMERCE_OPS_DEMO_MODE=false")

    with pytest.raises(module.DeploymentError, match="environment"):
        validate(module, inspection)


@pytest.mark.parametrize(
    "injected_environment",
    [
        "BASH_ENV=/app/data/hook",
        "PYTHONPATH=/app/data",
        "LD_PRELOAD=/app/data/inject.so",
    ],
)
def test_candidate_runtime_rejects_environment_outside_the_image_baseline(
    injected_environment: str,
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    config = inspection["Config"]
    assert isinstance(config, dict)
    environment = config["Env"]
    assert isinstance(environment, list)
    environment.append(injected_environment)

    with pytest.raises(module.DeploymentError, match="environment"):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("Cmd", ["python", "-m", "http.server", "8000"]),
        ("Entrypoint", ["/app/data/entrypoint"]),
        ("Healthcheck", {"Test": ["CMD", "true"]}),
        ("WorkingDir", "/app/data"),
        ("Volumes", {"/app/data": {}, "/host": {}}),
        ("ExposedPorts", {"8000/tcp": {}, "9000/tcp": {}}),
    ],
)
def test_candidate_runtime_rejects_image_config_overrides(
    field: str, override: object
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    config = inspection["Config"]
    assert isinstance(config, dict)
    config[field] = override

    with pytest.raises(module.DeploymentError, match="image configuration"):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("CapAdd", ["SYS_ADMIN"]),
        (
            "Devices",
            [
                {
                    "PathOnHost": "/dev/sda",
                    "PathInContainer": "/dev/sda",
                    "CgroupPermissions": "rwm",
                }
            ],
        ),
        ("DeviceRequests", [{"Capabilities": [["gpu"]]}]),
        ("VolumesFrom", ["host-container:rw"]),
        ("PidMode", "host"),
        ("IpcMode", "host"),
        ("UTSMode", "host"),
        ("UsernsMode", "host"),
        ("CgroupnsMode", "host"),
        ("NetworkMode", "host"),
    ],
)
def test_candidate_runtime_rejects_extra_privilege_and_host_namespaces(
    field: str, unsafe_value: object
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config[field] = unsafe_value

    with pytest.raises(module.DeploymentError, match="isolation"):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("Config", "User"),
        ("Config", "Entrypoint"),
        ("HostConfig", "Privileged"),
        ("HostConfig", "ReadonlyRootfs"),
        ("HostConfig", "CapDrop"),
        ("HostConfig", "SecurityOpt"),
        ("HostConfig", "Init"),
        ("HostConfig", "RestartPolicy"),
        ("HostConfig", "PortBindings"),
        ("HostConfig", "Tmpfs"),
    ],
)
def test_candidate_runtime_fails_closed_when_isolation_field_is_missing(
    section: str, key: str
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    target = inspection[section]
    assert isinstance(target, dict)
    del target[key]

    with pytest.raises(module.DeploymentError):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("key", "unsafe_value"),
    [
        ("Privileged", True),
        ("ReadonlyRootfs", False),
        ("CapDrop", []),
        ("CapDrop", ["ALL", "NET_RAW"]),
        ("SecurityOpt", []),
        ("SecurityOpt", ["no-new-privileges:false"]),
        ("Init", False),
        (
            "RestartPolicy",
            {"Name": "no", "MaximumRetryCount": 0},
        ),
        (
            "Tmpfs",
            {"/tmp": "rw,exec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=1777"},
        ),
        (
            "Tmpfs",
            {
                "/tmp": "rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=1777",
                "/host": "rw,size=64m",
            },
        ),
    ],
)
def test_candidate_runtime_rejects_weakened_isolation_settings(
    key: str, unsafe_value: object
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config[key] = copy.deepcopy(unsafe_value)

    with pytest.raises(module.DeploymentError):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("key", "unsafe_value"),
    [
        ("BlkioWeight", 1_000),
        ("MaskedPaths", []),
        ("MaskedPaths", ["/proc/kcore"]),
        ("ReadonlyPaths", []),
        ("ReadonlyPaths", ["/proc/sys"]),
    ],
)
def test_candidate_runtime_rejects_weakened_kernel_path_and_io_defaults(
    key: str,
    unsafe_value: object,
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config[key] = copy.deepcopy(unsafe_value)

    with pytest.raises(module.DeploymentError, match="host configuration"):
        validate(module, inspection)


def test_candidate_runtime_requires_exactly_one_writable_data_bind() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    mounts = inspection["Mounts"]
    assert isinstance(mounts, list)
    mount = mounts[0]
    assert isinstance(mount, dict)
    mount["RW"] = False

    with pytest.raises(module.DeploymentError, match="mount"):
        validate(module, inspection)


def test_candidate_runtime_requires_private_bind_propagation() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    mounts = inspection["Mounts"]
    assert isinstance(mounts, list)
    mount = mounts[0]
    assert isinstance(mount, dict)
    mount["Propagation"] = "rshared"

    with pytest.raises(module.DeploymentError, match="mount"):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("Mode", "rw"),
        ("FutureMountEscape", True),
        ("Mode", None),
    ],
)
def test_candidate_runtime_requires_the_exact_data_mount_contract(
    mutation: str,
    value: object,
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    mounts = inspection["Mounts"]
    assert isinstance(mounts, list)
    mount = mounts[0]
    assert isinstance(mount, dict)
    if value is None:
        del mount[mutation]
    else:
        mount[mutation] = value

    with pytest.raises(module.DeploymentError, match="mount"):
        validate(module, inspection)


def test_candidate_runtime_rejects_duplicate_critical_environment() -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    config = inspection["Config"]
    assert isinstance(config, dict)
    environment = config["Env"]
    assert isinstance(environment, list)
    environment.append("COMMERCE_OPS_ENVIRONMENT=development")

    with pytest.raises(module.DeploymentError, match="environment"):
        validate(module, inspection)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("AutoRemove", True),
        ("Binds", ["/:/host:rw"]),
        ("CgroupParent", "host.slice"),
        ("Dns", ["8.8.8.8"]),
        ("DnsOptions", ["use-vc"]),
        ("DnsSearch", ["internal.example"]),
        ("ExtraHosts", ["metadata:169.254.169.254"]),
        ("GroupAdd", ["999"]),
        ("Links", ["/host:/linked"]),
        ("MemorySwap", -1),
        ("OomKillDisable", True),
        ("PublishAllPorts", True),
        ("Runtime", "kata-runtime"),
        ("ShmSize", 1024 * 1024 * 1024),
        ("Sysctls", {"net.ipv4.ip_forward": "1"}),
        (
            "Ulimits",
            [{"Name": "nofile", "Hard": 1_048_576, "Soft": 1_048_576}],
        ),
        ("FutureIsolationEscape", True),
    ],
)
def test_candidate_runtime_rejects_unapproved_host_config_overrides(
    field: str,
    unsafe_value: object,
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    host_config[field] = copy.deepcopy(unsafe_value)

    with pytest.raises(module.DeploymentError, match="host configuration"):
        validate(module, inspection)


@pytest.mark.parametrize(
    "required_field",
    [
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
    ],
)
def test_candidate_runtime_requires_security_relevant_host_config_defaults(
    required_field: str,
) -> None:
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    host_config = inspection["HostConfig"]
    assert isinstance(host_config, dict)
    del host_config[required_field]

    with pytest.raises(module.DeploymentError, match="host configuration"):
        validate(module, inspection)


def test_candidate_runtime_hash_is_canonical_sensitive_and_excludes_network_state() -> (
    None
):
    module = load_deploy_tool()
    inspection = valid_inspection(module)
    canonical_hash = module._candidate_runtime_sha256(inspection)
    reordered = {key: inspection[key] for key in reversed(inspection)}

    assert re.fullmatch(r"[0-9a-f]{64}", canonical_hash)
    assert module._candidate_runtime_sha256(reordered) == canonical_hash

    changed_runtime = copy.deepcopy(inspection)
    host_config = changed_runtime["HostConfig"]
    assert isinstance(host_config, dict)
    host_config["Runtime"] = "kata-runtime"
    assert module._candidate_runtime_sha256(changed_runtime) != canonical_hash

    changed_network = copy.deepcopy(inspection)
    changed_network["NetworkSettings"] = {"Networks": {"attacker": {}}}
    assert module._candidate_runtime_sha256(changed_network) == canonical_hash


def test_candidate_runtime_revalidation_uses_exact_container_and_mount_identity() -> (
    None
):
    module = load_deploy_tool()
    identity = module.candidate_identity(VALID_SHA)
    container_id = "b" * 64
    data_identity = (64_769, 55_451_027, module.RUNTIME_UID, module.RUNTIME_GID)
    inspection = valid_inspection(module)
    inspection.update(
        {
            "Id": container_id,
            "Name": f"/{identity.container_name}",
        }
    )

    class RuntimeRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(
            self, arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            command = list(arguments)
            self.calls.append(command)
            if command == [module.DOCKER_BINARY, "inspect", container_id]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps([inspection]), ""
                )
            assert command == [
                module.DOCKER_BINARY,
                "exec",
                "--user",
                f"{module.RUNTIME_UID}:{module.RUNTIME_GID}",
                container_id,
                "/usr/bin/stat",
                "--dereference",
                "--format=%d|%i|%u|%g",
                "--",
                "/app/data",
            ]
            return subprocess.CompletedProcess(
                command,
                0,
                "|".join(str(value) for value in data_identity) + "\n",
                "",
            )

    runner = RuntimeRunner()
    runtime_sha256 = module._assert_candidate_runtime(
        runner,
        identity,
        expected_container_id=container_id,
        expected_image_id=IMAGE_ID,
        network_name=NETWORK_NAME,
        port=18088,
        data_directory=DATA_DIRECTORY,
        data_identity=data_identity,
        trusted_proxy_json=TRUSTED_PROXY_JSON,
        image_config=valid_image_config(),
    )

    assert runtime_sha256 == module._candidate_runtime_sha256(inspection)
    assert len(runner.calls) == 2
