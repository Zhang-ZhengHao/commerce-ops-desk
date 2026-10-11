"""Build a workstation image only from one approved Git commit object.

This is a local provenance control.  It keeps dirty and untracked working-tree
content out of the Docker context, but it is not a cryptographic signature or a
remote supply-chain attestation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

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


def _load_verified_provenance_module() -> ModuleType:
    module_path = Path(__file__).with_name("verified_provenance.py")
    module_name = "commerce_ops_verified_provenance_builder"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("verified provenance module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(module_name) is module:
            del sys.modules[module_name]
        raise
    return module


_PROVENANCE = _load_verified_provenance_module()


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


def _validated_repository(path: Path) -> Path:
    try:
        repository = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise BuildError("repository does not exist") from None
    if not repository.is_dir():
        raise BuildError("repository must be a directory")
    return repository


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
    validated_sha = validate_source_sha(source_sha)
    validated_ref = validate_approved_remote_ref(approved_remote_ref)
    image = f"{IMAGE_REPOSITORY}:{validated_sha}"

    with tempfile.TemporaryDirectory(prefix="commerce-ops-build-") as temporary:
        temporary_directory = Path(temporary)
        context = temporary_directory / "context"
        try:
            with _PROVENANCE.open_canonical_checkout_root(
                validated_repository
            ) as checkout:
                revision = _PROVENANCE.export_revision_evidence(
                    checkout,
                    validated_sha,
                    validated_ref,
                    workspace=temporary_directory,
                    extract_to=context,
                )
        except _PROVENANCE.ProvenanceError as error:
            raise BuildError(str(error)) from None
        deployment_asset_hashes = dict(revision.deployment_assets.sha256)
        docker_daemon_id = _docker_daemon_id(
            command_runner,
            repository=validated_repository,
        )
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
