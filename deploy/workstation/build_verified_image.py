"""Build a workstation image only from one approved Git commit object.

This is a local provenance control.  It keeps dirty and untracked working-tree
content out of the Docker context, but it is not a cryptographic signature or a
remote supply-chain attestation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
REMOTE_REF_PREFIX = "refs/remotes/"
COMMAND_TIMEOUT_SECONDS = 60.0
BUILD_TIMEOUT_SECONDS = 1_800.0
IMAGE_REPOSITORY = "commerce-ops-desk"
DOCKER_BINARY = "/usr/bin/docker"
SAFE_SYSTEM_PATH = "/usr/bin:/bin"
BUILD_MANIFEST_SCHEMA = 2
DEPLOYMENT_ASSET_SCHEMA = 1
DEPLOYMENT_ASSET_PATHS = (
    "deploy/workstation/compose.yaml",
    "deploy/workstation/commerce-ops-desk.Caddyfile.template",
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template",
)
GIT_COMMAND = ("git", "--no-replace-objects")


class BuildError(RuntimeError):
    """A safe error that does not include captured command output."""


@dataclass(frozen=True)
class BuiltImage:
    reference: str
    image_id: str
    source_sha: str


def _local_docker_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
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


def _prepare_private_directory(directory: Path) -> Path:
    if not directory.is_absolute():
        raise BuildError("build manifest path must be absolute")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
        resolved = directory.resolve(strict=True)
    except OSError:
        raise BuildError(
            "build manifest directory could not be prepared safely"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or resolved != directory
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise BuildError("build manifest directory is not private")
    try:
        directory.chmod(0o700)
    except OSError:
        raise BuildError("build manifest directory could not be secured") from None
    return directory


def _assert_replaceable_private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise BuildError("existing build manifest could not be inspected") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise BuildError("existing build manifest is not a private regular file")


def _write_build_manifest(
    path: Path,
    built: BuiltImage,
    approved_ref: str,
    deployment_asset_hashes: Mapping[str, str],
    docker_daemon_id: str,
) -> None:
    if not path.is_absolute() or not path.name:
        raise BuildError("build manifest path must be an absolute file path")
    directory = _prepare_private_directory(path.parent)
    _assert_replaceable_private_file(path)
    payload = {
        "approved_remote_ref": approved_ref,
        "deployment_assets": {
            "schema": DEPLOYMENT_ASSET_SCHEMA,
            "sha256": dict(deployment_asset_hashes),
        },
        "docker_daemon_id": docker_daemon_id,
        "image_id": built.image_id,
        "image_reference": built.reference,
        "schema": BUILD_MANIFEST_SCHEMA,
        "source_sha": built.source_sha,
    }
    temporary = directory / (f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class CommandRunner:
    """Run fixed argv commands while keeping subprocess output private."""

    def query(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        failure_message: str,
        environment: Mapping[str, str] | None = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ) -> str:
        if arguments and arguments[0] == DOCKER_BINARY:
            environment = _local_docker_environment()
        try:
            result = subprocess.run(
                list(arguments),
                cwd=cwd,
                env=None if environment is None else dict(environment),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BuildError(failure_message) from error
        if result.returncode != 0:
            raise BuildError(failure_message)
        return result.stdout.strip()

    def run_quietly(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        failure_message: str,
        timeout: float,
    ) -> None:
        environment = (
            _local_docker_environment()
            if arguments and arguments[0] == DOCKER_BINARY
            else None
        )
        try:
            result = subprocess.run(
                list(arguments),
                cwd=cwd,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BuildError(failure_message) from error
        if result.returncode != 0:
            raise BuildError(failure_message)

    def write_stdout(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        destination: Path,
        failure_message: str,
        environment: Mapping[str, str] | None = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        try:
            with destination.open("xb") as output:
                result = subprocess.run(
                    list(arguments),
                    cwd=cwd,
                    env=None if environment is None else dict(environment),
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=timeout,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BuildError(failure_message) from error
        if result.returncode != 0:
            raise BuildError(failure_message)


def validate_source_sha(value: str) -> str:
    if SOURCE_SHA_PATTERN.fullmatch(value) is None:
        raise BuildError("SOURCE_SHA must be one full 40-character lowercase Git SHA")
    return value


def validate_approved_remote_ref(value: str) -> str:
    if not value.startswith(REMOTE_REF_PREFIX) or value == REMOTE_REF_PREFIX:
        raise BuildError(
            "approved remote ref must be a full refs/remotes/<remote>/<branch> name"
        )
    return value


def _isolated_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _isolated_archive_environment(temporary_directory: Path) -> dict[str, str]:
    home = temporary_directory / "git-home"
    xdg_config = temporary_directory / "git-xdg-config"
    home.mkdir(mode=0o700)
    xdg_config.mkdir(mode=0o700)
    environment = _isolated_git_environment()
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg_config),
        }
    )
    return environment


def _validated_repository(path: Path) -> Path:
    try:
        repository = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise BuildError("repository does not exist") from None
    if not repository.is_dir():
        raise BuildError("repository must be a directory")
    return repository


def _assert_repository_toplevel(
    runner: CommandRunner,
    repository: Path,
) -> None:
    raw_toplevel = runner.query(
        [*GIT_COMMAND, "rev-parse", "--show-toplevel"],
        cwd=repository,
        environment=_isolated_git_environment(),
        failure_message="repository is not a Git worktree",
    )
    try:
        toplevel = Path(raw_toplevel).resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise BuildError("Git repository top level is unavailable") from None
    if toplevel != repository:
        raise BuildError("repository must be the exact Git repository top level")


def assert_approved_commit(
    runner: CommandRunner,
    *,
    repository: Path,
    source_sha: str,
    approved_remote_ref: str,
) -> None:
    git_environment = _isolated_git_environment()
    runner.query(
        [*GIT_COMMAND, "check-ref-format", approved_remote_ref],
        cwd=repository,
        failure_message="approved remote ref is not a valid Git ref",
        environment=git_environment,
    )
    object_type = runner.query(
        [*GIT_COMMAND, "cat-file", "-t", source_sha],
        cwd=repository,
        failure_message="SOURCE_SHA is not an available Git object",
        environment=git_environment,
    )
    if object_type != "commit":
        raise BuildError("SOURCE_SHA must name a commit object directly")
    approved_sha = runner.query(
        [*GIT_COMMAND, "show-ref", "--verify", "--hash", approved_remote_ref],
        cwd=repository,
        failure_message="approved remote ref is unavailable",
        environment=git_environment,
    )
    if approved_sha != source_sha:
        raise BuildError("SOURCE_SHA does not match the approved remote ref")


def _export_context(
    runner: CommandRunner,
    *,
    repository: Path,
    source_sha: str,
    temporary_directory: Path,
) -> Path:
    archive = temporary_directory / "source.tar"
    context = temporary_directory / "context"
    context.mkdir(mode=0o700)
    archive_environment = _isolated_archive_environment(temporary_directory)
    raw_object_directory = runner.query(
        [
            *GIT_COMMAND,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ],
        cwd=repository,
        failure_message="repository object database is unavailable",
        environment=_isolated_git_environment(),
    )
    try:
        object_directory = Path(raw_object_directory).resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise BuildError("repository object database is unavailable") from None
    if not object_directory.is_dir():
        raise BuildError("repository object database is unavailable")

    object_view = temporary_directory / "source.git"
    runner.query(
        [
            *GIT_COMMAND,
            "-c",
            "init.templateDir=",
            "init",
            "--bare",
            str(object_view),
        ],
        cwd=temporary_directory,
        failure_message="isolated Git object view could not be prepared",
        environment=archive_environment,
    )
    alternates = object_view / "objects" / "info" / "alternates"
    try:
        alternates.write_text(f"{object_directory}\n", encoding="utf-8")
    except OSError:
        raise BuildError("isolated Git object view could not be prepared") from None

    runner.write_stdout(
        [
            *GIT_COMMAND,
            f"--git-dir={object_view}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "archive",
            "--format=tar",
            source_sha,
        ],
        cwd=temporary_directory,
        destination=archive,
        failure_message="git archive failed",
        environment=archive_environment,
    )
    try:
        with tarfile.open(archive, mode="r:") as source:
            source.extractall(context, filter="data")
    except (OSError, tarfile.TarError, tarfile.FilterError):
        raise BuildError("approved Git archive could not be extracted safely") from None
    finally:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
    if not (context / "Dockerfile").is_file():
        raise BuildError("approved Git archive does not contain Dockerfile")
    return context


def _hash_approved_deployment_assets(context: Path) -> dict[str, str]:
    try:
        context_root = context.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise BuildError("approved deployment assets are unavailable") from None
    digests: dict[str, str] = {}
    for relative_path in DEPLOYMENT_ASSET_PATHS:
        path = context / relative_path
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
            content = path.read_bytes()
        except (FileNotFoundError, OSError):
            raise BuildError(
                f"approved deployment asset is unavailable: {relative_path}"
            ) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or resolved != path
            or not resolved.is_relative_to(context_root)
        ):
            raise BuildError(
                f"approved deployment asset is not a regular file: {relative_path}"
            )
        digests[relative_path] = hashlib.sha256(content).hexdigest()
    return digests


def _inspect_built_image(
    runner: CommandRunner,
    *,
    repository: Path,
    image: str,
    source_sha: str,
) -> BuiltImage:
    raw_inspection = runner.query(
        [DOCKER_BINARY, "image", "inspect", image],
        cwd=repository,
        failure_message="built image identity could not be inspected",
    )
    try:
        inspection = json.loads(raw_inspection)
    except json.JSONDecodeError:
        raise BuildError("built image inspection returned invalid JSON") from None
    if (
        not isinstance(inspection, list)
        or len(inspection) != 1
        or not isinstance(inspection[0], dict)
    ):
        raise BuildError("built image inspection returned an invalid shape")
    image_document = inspection[0]
    config = image_document.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    revision = (
        labels.get("org.opencontainers.image.revision")
        if isinstance(labels, dict)
        else None
    )
    if revision != source_sha:
        raise BuildError("built image OCI revision label does not match SOURCE_SHA")
    environment = config.get("Env") if isinstance(config, dict) else None
    runtime_source_shas = (
        [
            entry.partition("=")[2]
            for entry in environment
            if isinstance(entry, str) and entry.startswith("COMMERCE_OPS_SOURCE_SHA=")
        ]
        if isinstance(environment, list)
        else []
    )
    if not runtime_source_shas:
        raise BuildError("built image runtime source SHA is missing")
    if len(runtime_source_shas) > 1:
        raise BuildError("built image runtime source SHA is defined more than once")
    if runtime_source_shas[0] != source_sha:
        raise BuildError("built image runtime source SHA does not match SOURCE_SHA")
    image_id = image_document.get("Id")
    if not isinstance(image_id, str) or IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise BuildError("built image did not return one immutable image ID")
    return BuiltImage(reference=image, image_id=image_id, source_sha=source_sha)


def _docker_daemon_id(runner: CommandRunner, *, repository: Path) -> str:
    raw_identifier = runner.query(
        [DOCKER_BINARY, "info", "--format", "{{json .ID}}"],
        cwd=repository,
        failure_message="local Docker daemon identity could not be inspected",
    )
    try:
        identifier = json.loads(raw_identifier)
    except json.JSONDecodeError:
        raise BuildError("local Docker daemon identity is invalid") from None
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier) <= 256
        or any(ord(character) < 33 or ord(character) > 126 for character in identifier)
    ):
        raise BuildError("local Docker daemon identity is invalid")
    return identifier


def build_verified_image(
    *,
    repository: Path,
    source_sha: str,
    approved_remote_ref: str,
    build_manifest: Path,
    runner: CommandRunner | None = None,
) -> BuiltImage:
    command_runner = runner or CommandRunner()
    validated_repository = _validated_repository(repository)
    _assert_repository_toplevel(command_runner, validated_repository)
    validated_sha = validate_source_sha(source_sha)
    validated_ref = validate_approved_remote_ref(approved_remote_ref)
    assert_approved_commit(
        command_runner,
        repository=validated_repository,
        source_sha=validated_sha,
        approved_remote_ref=validated_ref,
    )
    image = f"{IMAGE_REPOSITORY}:{validated_sha}"
    docker_daemon_id = _docker_daemon_id(
        command_runner,
        repository=validated_repository,
    )

    with tempfile.TemporaryDirectory(prefix="commerce-ops-build-") as temporary:
        context = _export_context(
            command_runner,
            repository=validated_repository,
            source_sha=validated_sha,
            temporary_directory=Path(temporary),
        )
        deployment_asset_hashes = _hash_approved_deployment_assets(context)
        command_runner.run_quietly(
            [
                DOCKER_BINARY,
                "build",
                "--build-arg",
                f"SOURCE_SHA={validated_sha}",
                "--tag",
                image,
                str(context),
            ],
            cwd=validated_repository,
            failure_message="docker build failed",
            timeout=BUILD_TIMEOUT_SECONDS,
        )

    built = _inspect_built_image(
        command_runner,
        repository=validated_repository,
        image=image,
        source_sha=validated_sha,
    )
    if (
        _docker_daemon_id(command_runner, repository=validated_repository)
        != docker_daemon_id
    ):
        raise BuildError("local Docker daemon identity changed during the build")
    _write_build_manifest(
        build_manifest,
        built,
        validated_ref,
        deployment_asset_hashes,
        docker_daemon_id,
    )
    return built


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build CommerceOps from an approved Git object, never the worktree."
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--approved-remote-ref", required=True)
    parser.add_argument("--build-manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        built = build_verified_image(
            repository=Path(arguments.repository),
            source_sha=arguments.source_sha,
            approved_remote_ref=arguments.approved_remote_ref,
            build_manifest=Path(arguments.build_manifest),
        )
    except BuildError as error:
        print(f"verified image build refused: {error}", file=sys.stderr)
        return 1
    print(
        f"verified local image built: {built.reference} "
        f"({built.image_id}, source {built.source_sha}); "
        f"private manifest: {arguments.build_manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
