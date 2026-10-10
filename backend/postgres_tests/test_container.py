"""Runtime proof for the hardened production container against PostgreSQL."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

from postgres_tests.harness import (
    TemporaryPostgresDatabase,
    redact_database_credentials,
)

CONTAINER_IMAGE_ENV = "COMMERCE_OPS_CONTAINER_IMAGE"
SOURCE_SHA_ENV = "COMMERCE_OPS_SOURCE_SHA"
CONTAINER_DATABASE_HOST = "host.docker.internal"
CONTAINER_PORT = 18_137
CONTAINER_TIMEOUT_SECONDS = 120.0
COMMAND_TIMEOUT_SECONDS = 15.0
DOCKER_RUN_TIMEOUT_SECONDS = 30.0
HEALTH_POLL_SECONDS = 0.5
EXPECTED_REVISION = "0006_maintenance_indexes"
DATA_TMPFS_TARGET = "/app/data"
DATA_TMPFS_OPTIONS = (
    "rw",
    "noexec",
    "nosuid",
    "nodev",
    "uid=10001",
    "gid=10001",
    "mode=0700",
)
RUNTIME_TMPFS_TARGET = "/tmp"
RUNTIME_TMPFS_OPTIONS = (
    "rw",
    "noexec",
    "nosuid",
    "nodev",
    "uid=10001",
    "gid=10001",
    "mode=1777",
    "size=64m",
)
ALLOWED_HOSTS_VALUE = '["127.0.0.1","localhost"]'
SENSITIVE_ENVIRONMENT_NAMES = (
    "COMMERCE_OPS_DATABASE_URL",
    "COMMERCE_OPS_SESSION_SECRET",
)
RUNTIME_ENVIRONMENT_NAMES = (
    *SENSITIVE_ENVIRONMENT_NAMES,
    "COMMERCE_OPS_ENVIRONMENT",
    "COMMERCE_OPS_ALLOWED_HOSTS",
    "PORT",
)
ABSENT_IMAGE_PATHS = (
    "/app/backend/tests",
    "/app/backend/postgres_tests",
    "/app/scripts/tests",
)


class ContainerProofError(RuntimeError):
    """Raised with a credential-safe container proof diagnostic."""


@dataclass(frozen=True)
class DiagnosticRedactor:
    database_urls: tuple[URL, ...] = field(repr=False)
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def redact(self, output: str) -> str:
        redacted = output
        for database_url in self.database_urls:
            redacted = redact_database_credentials(redacted, database_url)
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContainerProofError(message)


def _coerce_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def _run_checked(
    command: Sequence[str],
    *,
    label: str,
    redactor: DiagnosticRedactor,
    environment: Mapping[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            env=None if environment is None else dict(environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=max(timeout, 0.1),
        )
    except subprocess.TimeoutExpired as error:
        captured = _coerce_output(error.stdout) + _coerce_output(error.stderr)
        diagnostic = redactor.redact(captured).strip()
        detail = f"\n{diagnostic}" if diagnostic else ""
        raise ContainerProofError(f"{label} timed out{detail}") from None
    except OSError as error:
        diagnostic = redactor.redact(str(error))
        raise ContainerProofError(f"{label} could not start: {diagnostic}") from None

    if result.returncode != 0:
        diagnostic = redactor.redact(result.stdout + result.stderr).strip()
        detail = f"\n{diagnostic}" if diagnostic else ""
        raise ContainerProofError(
            f"{label} failed with exit code {result.returncode}{detail}"
        ) from None
    return result


def _remaining_timeout(deadline: float, maximum: float = COMMAND_TIMEOUT_SECONDS) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContainerProofError("Container proof exceeded its overall timeout")
    return min(remaining, maximum)


def _parse_json(raw: str, *, label: str) -> object:
    try:
        return cast(object, json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        raise ContainerProofError(f"{label} did not return valid JSON") from None


def _as_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContainerProofError(f"{label} did not return a JSON object")
    return cast(dict[str, Any], value)


def _as_list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContainerProofError(f"{label} did not return a JSON array")
    return value


def _docker_json(
    docker: str,
    arguments: Sequence[str],
    *,
    label: str,
    redactor: DiagnosticRedactor,
    deadline: float,
) -> object:
    result = _run_checked(
        [docker, *arguments],
        label=label,
        redactor=redactor,
        timeout=_remaining_timeout(deadline),
    )
    return _parse_json(result.stdout, label=label)


def _wait_until_healthy(
    docker: str,
    container_name: str,
    *,
    redactor: DiagnosticRedactor,
    deadline: float,
) -> None:
    while True:
        state_value = _docker_json(
            docker,
            ["inspect", "--format", "{{json .State}}", container_name],
            label="Docker health inspection",
            redactor=redactor,
            deadline=deadline,
        )
        state = _as_mapping(state_value, label="Docker health inspection")
        health = _as_mapping(state.get("Health"), label="Docker HEALTHCHECK state")
        health_status = health.get("Status")

        if health_status == "healthy":
            return
        if health_status == "unhealthy":
            raise ContainerProofError("Container HEALTHCHECK reported unhealthy")
        if state.get("Status") in {"dead", "exited", "removing"}:
            raise ContainerProofError("Container exited before its HEALTHCHECK became healthy")

        wait_seconds = min(HEALTH_POLL_SECONDS, _remaining_timeout(deadline))
        time.sleep(wait_seconds)


def _inspect_container(
    docker: str,
    container_name: str,
    *,
    redactor: DiagnosticRedactor,
    deadline: float,
) -> dict[str, Any]:
    value = _docker_json(
        docker,
        ["inspect", container_name],
        label="Docker container inspection",
        redactor=redactor,
        deadline=deadline,
    )
    if not isinstance(value, list) or len(value) != 1:
        raise ContainerProofError("Docker inspect did not return exactly one container")
    return _as_mapping(value[0], label="Docker container inspection")


def _single_binding(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1:
        raise ContainerProofError(f"{label} must contain exactly one binding")
    return _as_mapping(value[0], label=label)


def _assert_container_configuration(
    inspection: dict[str, Any],
    *,
    container_name: str,
    container_id: str,
    image: str,
    source_sha: str,
    database_url: str,
    session_secret: str,
) -> int:
    _require(inspection.get("Name") == f"/{container_name}", "Container name changed")
    _require(inspection.get("Id") == container_id, "Docker inspected a different container")

    config = _as_mapping(inspection.get("Config"), label="Docker Config")
    configured_user = config.get("User")
    _require(
        isinstance(configured_user, str)
        and configured_user not in {"", "0", "0:0", "root", "root:root"},
        "Container image is configured to run as root",
    )
    _require(config.get("Image") == image, "Docker ran an unexpected image")
    labels = _as_mapping(config.get("Labels"), label="Docker image labels")
    _require(
        labels.get("org.opencontainers.image.revision") == source_sha,
        "Container image revision does not match the tested source SHA",
    )

    environment_entries = _as_list(config.get("Env"), label="Container environment")
    container_environment = {
        entry.partition("=")[0]: entry.partition("=")[2]
        for entry in environment_entries
        if isinstance(entry, str) and "=" in entry
    }
    _require(
        container_environment.get("COMMERCE_OPS_DATABASE_URL") == database_url,
        "Container did not receive the temporary PostgreSQL database URL",
    )
    _require(
        container_environment.get("COMMERCE_OPS_SESSION_SECRET") == session_secret,
        "Container did not receive the test session secret",
    )
    _require(
        container_environment.get(SOURCE_SHA_ENV) == source_sha,
        "Container runtime source SHA does not match the tested source SHA",
    )
    _require(
        container_environment.get("COMMERCE_OPS_ENVIRONMENT") == "production",
        "Container is not running with the production environment boundary",
    )
    _require(
        container_environment.get("COMMERCE_OPS_ALLOWED_HOSTS") == ALLOWED_HOSTS_VALUE,
        "Container does not enforce the expected local Host boundary",
    )
    _require(
        container_environment.get("PORT") == str(CONTAINER_PORT),
        "Container did not receive the selected internal port",
    )

    host_config = _as_mapping(inspection.get("HostConfig"), label="Docker HostConfig")
    _require(host_config.get("Privileged") is False, "Container is privileged")
    _require(host_config.get("ReadonlyRootfs") is True, "Root filesystem is writable")
    _require(host_config.get("NanoCpus") == 1_000_000_000, "Container CPU limit changed")
    _require(host_config.get("Memory") == 512 * 1024 * 1024, "Container memory limit changed")
    _require(host_config.get("PidsLimit") == 128, "Container PID limit changed")
    restart_policy = _as_mapping(
        host_config.get("RestartPolicy"),
        label="Docker restart policy",
    )
    _require(
        restart_policy.get("Name") == "unless-stopped",
        "Container restart policy changed",
    )
    log_config = _as_mapping(host_config.get("LogConfig"), label="Docker log config")
    _require(log_config.get("Type") == "local", "Container log driver is not local")
    _require(
        log_config.get("Config") == {"max-size": "10m", "max-file": "3"},
        "Container log rotation options changed",
    )

    cap_drop = host_config.get("CapDrop")
    _require(
        isinstance(cap_drop, list)
        and {str(capability).upper() for capability in cap_drop} == {"ALL"},
        "Container does not drop every Linux capability",
    )
    _require(
        host_config.get("SecurityOpt") == ["no-new-privileges=true"],
        "Container does not enforce no-new-privileges",
    )
    _require(host_config.get("Binds") in (None, []), "Container has a bind mount")

    tmpfs = _as_mapping(host_config.get("Tmpfs"), label="Docker tmpfs configuration")
    _require(
        set(tmpfs) == {DATA_TMPFS_TARGET, RUNTIME_TMPFS_TARGET},
        "Container tmpfs targets changed",
    )
    tmpfs_options = tmpfs.get(DATA_TMPFS_TARGET)
    if not isinstance(tmpfs_options, str):
        raise ContainerProofError("Container data tmpfs options are unavailable")
    _require(
        set(tmpfs_options.split(",")) == set(DATA_TMPFS_OPTIONS),
        "Container data tmpfs is not hardened as required",
    )
    runtime_tmpfs_options = tmpfs.get(RUNTIME_TMPFS_TARGET)
    if not isinstance(runtime_tmpfs_options, str):
        raise ContainerProofError("Container runtime tmpfs options are unavailable")
    _require(
        set(runtime_tmpfs_options.split(",")) == set(RUNTIME_TMPFS_OPTIONS),
        "Container runtime tmpfs is not hardened as required",
    )

    mounts = _as_list(inspection.get("Mounts"), label="Docker mounts")
    _require(len(mounts) <= 2, "Unexpected container mount")
    mount_shapes = {
        (
            _as_mapping(mount, label="Docker mount").get("Type"),
            _as_mapping(mount, label="Docker mount").get("Destination"),
        )
        for mount in mounts
    }
    _require(
        mount_shapes
        <= {
            ("tmpfs", DATA_TMPFS_TARGET),
            ("tmpfs", RUNTIME_TMPFS_TARGET),
        },
        "Container reported an unexpected mount",
    )

    port_key = f"{CONTAINER_PORT}/tcp"
    port_bindings = _as_mapping(
        host_config.get("PortBindings"),
        label="Docker host port bindings",
    )
    _require(set(port_bindings) == {port_key}, "Container publishes an unexpected port")
    _single_binding(port_bindings.get(port_key), label="Docker host port binding")

    network_settings = _as_mapping(
        inspection.get("NetworkSettings"),
        label="Docker NetworkSettings",
    )
    network_ports = _as_mapping(
        network_settings.get("Ports"),
        label="Docker network port bindings",
    )
    published_ports = {key for key, bindings in network_ports.items() if bindings}
    _require(published_ports == {port_key}, "Container publishes an unexpected network port")
    network_binding = _single_binding(
        network_ports.get(port_key),
        label="Docker network port binding",
    )
    _require(
        network_binding.get("HostIp") == "127.0.0.1",
        "Container port is not restricted to host loopback",
    )
    host_port = network_binding.get("HostPort")
    if not isinstance(host_port, str):
        raise ContainerProofError("Docker did not allocate a valid ephemeral host port")
    _require(
        host_port.isascii() and host_port.isdigit() and 1 <= int(host_port) <= 65_535,
        "Docker did not allocate a valid ephemeral host port",
    )
    return int(host_port)


def _assert_ready(host_port: int, *, deadline: float) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{host_port}/ready",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=_remaining_timeout(deadline, 5.0)) as response:
            status = response.status
            body = response.read(4_097)
    except OSError as error:
        raise ContainerProofError(f"Host readiness request failed: {error}") from None

    _require(status == 200, "Host readiness request did not return HTTP 200")
    _require(len(body) <= 4_096, "Host readiness response exceeded its size boundary")
    try:
        payload = cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ContainerProofError("Host readiness response was not valid JSON") from None
    _require(
        payload == {"status": "ready", "database": "reachable"},
        "Host readiness response did not match the exact contract",
    )


def _assert_build_metadata(
    host_port: int,
    *,
    source_sha: str,
    deadline: float,
) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{host_port}/api/build",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=_remaining_timeout(deadline, 5.0)) as response:
            status = response.status
            cache_control = response.headers.get("Cache-Control")
            body = response.read(4_097)
    except OSError as error:
        raise ContainerProofError(f"Host build metadata request failed: {error}") from None

    _require(status == 200, "Host build metadata request did not return HTTP 200")
    _require(cache_control == "no-store", "Host build metadata response is cacheable")
    _require(len(body) <= 4_096, "Host build metadata response exceeded its size boundary")
    try:
        payload = cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ContainerProofError("Host build metadata response was not valid JSON") from None
    _require(
        payload
        == {
            "service": "commerce-ops-desk",
            "version": "0.2.1",
            "source_sha": source_sha,
        },
        "Host build metadata did not match the tested source SHA",
    )


def _assert_runtime_process(
    docker: str,
    container_name: str,
    *,
    redactor: DiagnosticRedactor,
    deadline: float,
) -> None:
    absent_paths = json.dumps(ABSENT_IMAGE_PATHS)
    probe = f"""
import json
import os

status = {{}}
with open("/proc/self/status", encoding="utf-8") as status_file:
    for line in status_file:
        key, separator, value = line.partition(":")
        if separator and key in {{"CapEff", "NoNewPrivs"}}:
            status[key] = value.strip()

print(json.dumps({{
    "uid": os.geteuid(),
    "cap_eff": status.get("CapEff"),
    "no_new_privs": status.get("NoNewPrivs"),
    "present_test_paths": [path for path in {absent_paths} if os.path.lexists(path)],
}}))
"""
    value = _docker_json(
        docker,
        [
            "exec",
            container_name,
            "/opt/venv/bin/python",
            "-c",
            probe,
        ],
        label="Container runtime security probe",
        redactor=redactor,
        deadline=deadline,
    )
    runtime = _as_mapping(value, label="Container runtime security probe")
    _require(runtime.get("uid") == 10_001, "Runtime process does not use uid 10001")
    _require(
        runtime.get("cap_eff") == "0000000000000000",
        "Runtime process retains an effective Linux capability",
    )
    _require(
        runtime.get("no_new_privs") == "1",
        "Runtime process does not enforce NoNewPrivs",
    )
    _require(
        runtime.get("present_test_paths") == [],
        "Runtime image contains excluded test source",
    )


def _container_logs(
    docker: str,
    container_name: str,
    *,
    redactor: DiagnosticRedactor,
) -> str:
    try:
        result = subprocess.run(
            [docker, "logs", "--tail", "200", container_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return redactor.redact(f"Container logs unavailable: {error}")

    output = redactor.redact(result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return output or "[container produced no logs]"
    return output or "[container logs unavailable]"


def _remove_container(
    docker: str,
    container_name: str,
    *,
    redactor: DiagnosticRedactor,
) -> str | None:
    try:
        result = subprocess.run(
            [docker, "rm", "--force", container_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return redactor.redact(f"Could not remove the test container: {error}")

    output = redactor.redact(result.stdout + result.stderr).strip()
    if result.returncode == 0 or "No such container" in output:
        return None
    return f"Could not remove the test container: {output or 'Docker returned an error'}"


@pytest.mark.container
def test_hardened_container_migrates_and_serves_postgresql(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        revision_table = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
    if revision_table is not None:
        pytest.fail("Temporary PostgreSQL database was already migrated", pytrace=False)

    image = os.environ.get(CONTAINER_IMAGE_ENV)
    if not image:
        pytest.fail(f"{CONTAINER_IMAGE_ENV} must name the image under test", pytrace=False)
    if image != image.strip():
        pytest.fail(f"{CONTAINER_IMAGE_ENV} must not contain surrounding whitespace", pytrace=False)
    source_sha = os.environ.get(SOURCE_SHA_ENV)
    if (
        source_sha is None
        or len(source_sha) != 40
        or any(character not in "0123456789abcdef" for character in source_sha)
    ):
        pytest.fail(f"{SOURCE_SHA_ENV} must be a full lowercase source SHA", pytrace=False)

    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("docker CLI is required for the container proof", pytrace=False)

    preflight_redactor = DiagnosticRedactor((postgres_database.url,))
    daemon_failure: str | None = None
    try:
        _run_checked(
            [docker, "info", "--format", "{{json .ServerVersion}}"],
            label="Docker daemon probe",
            redactor=preflight_redactor,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except ContainerProofError as error:
        daemon_failure = str(error)
    if daemon_failure is not None:
        pytest.fail(daemon_failure, pytrace=False)

    container_database_url = postgres_database.url.set(host=CONTAINER_DATABASE_HOST)
    _require(
        container_database_url.host == CONTAINER_DATABASE_HOST,
        "Container database URL host rewrite failed",
    )
    rendered_database_url = container_database_url.render_as_string(hide_password=False)
    session_secret = secrets.token_urlsafe(48)
    redactor = DiagnosticRedactor(
        (postgres_database.url, container_database_url),
        (session_secret,),
    )

    container_name = f"commerce-ops-proof-{uuid4().hex}"
    runtime_environment = os.environ.copy()
    runtime_environment.update(
        {
            "COMMERCE_OPS_DATABASE_URL": rendered_database_url,
            "COMMERCE_OPS_SESSION_SECRET": session_secret,
            "COMMERCE_OPS_ENVIRONMENT": "production",
            "COMMERCE_OPS_ALLOWED_HOSTS": ALLOWED_HOSTS_VALUE,
            "PORT": str(CONTAINER_PORT),
        }
    )
    run_command = [
        docker,
        "run",
        "--detach",
        "--name",
        container_name,
        "--restart",
        "unless-stopped",
        "--cpus",
        "1.0",
        "--memory",
        "512m",
        "--pids-limit",
        "128",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        "--read-only",
        "--tmpfs",
        f"{DATA_TMPFS_TARGET}:{','.join(DATA_TMPFS_OPTIONS)}",
        "--tmpfs",
        f"{RUNTIME_TMPFS_TARGET}:{','.join(RUNTIME_TMPFS_OPTIONS)}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--add-host",
        f"{CONTAINER_DATABASE_HOST}:host-gateway",
        "--publish",
        f"127.0.0.1::{CONTAINER_PORT}",
    ]
    for environment_name in RUNTIME_ENVIRONMENT_NAMES:
        run_command.extend(("-e", environment_name))
    run_command.append(image)

    _require(
        SOURCE_SHA_ENV not in RUNTIME_ENVIRONMENT_NAMES and SOURCE_SHA_ENV not in run_command,
        "Container proof must inherit the source SHA from the image baseline",
    )

    command_text = "\0".join(run_command)
    _require(
        rendered_database_url not in command_text and session_secret not in command_text,
        "A sensitive runtime value was placed in Docker argv",
    )
    _require(CONTAINER_PORT != 8_000, "Container proof must not use the image's default port")

    deadline = time.monotonic() + CONTAINER_TIMEOUT_SECONDS
    body_failure: Exception | None = None
    container_logs = ""
    container_attempted = False
    cleanup_failure: str | None = None
    try:
        container_attempted = True
        run_result = _run_checked(
            run_command,
            label="Docker container start",
            redactor=redactor,
            environment=runtime_environment,
            timeout=_remaining_timeout(deadline, DOCKER_RUN_TIMEOUT_SECONDS),
        )
        container_id = run_result.stdout.strip()
        _require(
            len(container_id) == 64
            and container_id.isascii()
            and all(character in "0123456789abcdef" for character in container_id),
            "Docker run did not return one full container id",
        )

        _wait_until_healthy(
            docker,
            container_name,
            redactor=redactor,
            deadline=deadline,
        )
        inspection = _inspect_container(
            docker,
            container_name,
            redactor=redactor,
            deadline=deadline,
        )
        host_port = _assert_container_configuration(
            inspection,
            container_name=container_name,
            container_id=container_id,
            image=image,
            source_sha=source_sha,
            database_url=rendered_database_url,
            session_secret=session_secret,
        )
        _assert_ready(host_port, deadline=deadline)
        _assert_build_metadata(
            host_port,
            source_sha=source_sha,
            deadline=deadline,
        )

        with postgres_engine.connect() as connection:
            applied_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        _require(
            applied_revision == EXPECTED_REVISION,
            "Container entrypoint did not migrate PostgreSQL to the expected revision",
        )
        _assert_runtime_process(
            docker,
            container_name,
            redactor=redactor,
            deadline=deadline,
        )
    except Exception as error:
        body_failure = error
        if container_attempted:
            container_logs = _container_logs(
                docker,
                container_name,
                redactor=redactor,
            )
    finally:
        if container_attempted:
            cleanup_failure = _remove_container(
                docker,
                container_name,
                redactor=redactor,
            )

    if body_failure is not None:
        diagnostic = redactor.redact(str(body_failure))
        if container_logs:
            diagnostic = f"{diagnostic}\nContainer logs (redacted):\n{container_logs}"
        if cleanup_failure is not None:
            diagnostic = f"{diagnostic}\n{cleanup_failure}"
        pytest.fail(diagnostic, pytrace=False)
    if cleanup_failure is not None:
        pytest.fail(cleanup_failure, pytrace=False)
