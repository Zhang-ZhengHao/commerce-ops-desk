"""Behavioral contracts for the workstation's verified image builder."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
BUILD_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "build_verified_image.py"
RUNBOOK = PRODUCT_ROOT / "deploy" / "workstation" / "RUNBOOK.md"
MAKEFILE = PRODUCT_ROOT / "Makefile"
WORKFLOW = PRODUCT_ROOT / ".github" / "workflows" / "verify.yml"
APPROVED_REF = "refs/remotes/origin/main"
DEPLOYMENT_ASSET_CONTENTS = {
    "deploy/workstation/compose.yaml": "services:\n  commerce-ops-desk:\n    image: demo\n",
    "deploy/workstation/commerce-ops-desk.Caddyfile.template": (
        "http://commerce-ops-desk.srrsh.aig.rest {\n"
        "\treverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n"
        "}\n"
    ),
    "deploy/workstation/commerce-ops-desk.v0.2.0.Caddyfile.template": (
        "http://commerce-ops-desk.srrsh.aig.rest {\n"
        "\treverse_proxy 127.0.0.1:{{UPSTREAM_PORT}}\n"
        "}\n"
    ),
}


def _run(
    *arguments: str,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def load_build_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_verified_image_build", BUILD_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> str:
    result = _run("git", *arguments, cwd=repository)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _create_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Build Test")
    _git(
        repository,
        "config",
        "user.email",
        "build-test@users.noreply.github.com",
    )
    (repository / "Dockerfile").write_text(
        "FROM scratch\nARG SOURCE_SHA\nLABEL org.opencontainers.image.revision=$SOURCE_SHA\n",
        encoding="utf-8",
    )
    (repository / "tracked.txt").write_text("approved bytes\n", encoding="utf-8")
    for relative_path, content in DEPLOYMENT_ASSET_CONTENTS.items():
        asset = repository / relative_path
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(content, encoding="utf-8")
    _git(repository, "add", "Dockerfile", "tracked.txt", "deploy")
    _git(repository, "commit", "--message", "approved source")
    source_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", APPROVED_REF, source_sha)
    return repository, source_sha


def _install_fake_docker(tmp_path: Path) -> Path:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    fake = binary_directory / "docker"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
if arguments[:1] == ["info"]:
    print(json.dumps(os.environ.get("FAKE_DOCKER_DAEMON_ID", "local-daemon-id")))
    raise SystemExit(0)

if arguments and arguments[0] == "build":
    context = Path(arguments[-1])
    capture = Path(os.environ["FAKE_DOCKER_CAPTURE"])
    tracked = context / "tracked.txt"
    payload = {
        "arguments": arguments,
        "context": str(context),
        "files": sorted(
            str(path.relative_to(context))
            for path in context.rglob("*")
            if path.is_file() or path.is_symlink()
        ),
        "tracked": tracked.read_text(encoding="utf-8") if tracked.is_file() else None,
    }
    capture.write_text(json.dumps(payload), encoding="utf-8")
    if os.environ.get("FAKE_DOCKER_BUILD_FAILURE") == "1":
        print(os.environ.get("SECRET_CANARY", "hidden"), file=sys.stderr)
        raise SystemExit(17)
    raise SystemExit(0)

if arguments[:2] == ["image", "inspect"]:
    inspect_capture = os.environ.get("FAKE_DOCKER_INSPECT_CAPTURE")
    if inspect_capture:
        with Path(inspect_capture).open("a", encoding="utf-8") as output:
            output.write(json.dumps(arguments) + "\\n")
    if "--format" not in arguments:
        print(json.dumps([{
            "Id": os.environ.get("FAKE_DOCKER_IMAGE_ID", "sha256:" + "a" * 64),
            "Config": {"Labels": {
                "org.opencontainers.image.revision": os.environ.get(
                    "FAKE_DOCKER_LABEL", os.environ["EXPECTED_SOURCE_SHA"]
                )
            }},
        }]))
        raise SystemExit(0)
    if os.environ.get("FAKE_DOCKER_REQUIRE_JSON_INSPECT") == "1":
        raise SystemExit(18)
    template = arguments[arguments.index("--format") + 1]
    if "revision" in template:
        print(os.environ.get("FAKE_DOCKER_LABEL", os.environ["EXPECTED_SOURCE_SHA"]))
    elif ".Id" in template:
        print(os.environ.get("FAKE_DOCKER_IMAGE_ID", "sha256:" + "a" * 64))
    else:
        raise SystemExit(3)
    raise SystemExit(0)

raise SystemExit(4)
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _tool_environment(
    tmp_path: Path,
    source_sha: str,
    capture: Path,
    **overrides: str,
) -> dict[str, str]:
    fake_docker = _install_fake_docker(tmp_path)
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_docker.parent}{os.pathsep}{environment['PATH']}",
            "TMPDIR": str(temporary_root),
            "FAKE_DOCKER_CAPTURE": str(capture),
            "FAKE_DOCKER_BINARY": str(fake_docker),
            "EXPECTED_SOURCE_SHA": source_sha,
            **overrides,
        }
    )
    return environment


def _build(
    repository: Path,
    source_sha: str,
    environment: dict[str, str],
    *,
    approved_ref: str = APPROVED_REF,
    build_manifest: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        str(BUILD_TOOL),
        "--repository",
        str(repository),
        "--source-sha",
        source_sha,
        "--approved-remote-ref",
        approved_ref,
    ]
    manifest = build_manifest or repository.parent / f"build-{source_sha}.json"
    arguments.extend(("--build-manifest", str(manifest)))
    entrypoint = """
import importlib.util
import os
from pathlib import Path
import sys

tool_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("verified_build_test_entrypoint", tool_path)
if spec is None or spec.loader is None:
    raise SystemExit(97)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.DOCKER_BINARY = os.environ["FAKE_DOCKER_BINARY"]
raise SystemExit(module.main(sys.argv[2:]))
"""
    return _run(
        sys.executable,
        "-c",
        entrypoint,
        str(BUILD_TOOL),
        *arguments[2:],
        cwd=repository,
        environment=environment,
    )


def test_dirty_worktree_builds_the_approved_committed_bytes(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    (repository / "tracked.txt").write_text("dirty bytes\n", encoding="utf-8")
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"
    assert not Path(payload["context"]).exists()


def test_replace_ref_cannot_substitute_the_approved_commit_tree(
    tmp_path: Path,
) -> None:
    repository, source_sha = _create_repository(tmp_path)
    (repository / "tracked.txt").write_text("replacement bytes\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--message", "local replacement")
    replacement_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "replace", source_sha, replacement_sha)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"


def test_repository_info_attributes_cannot_export_ignore_approved_files(
    tmp_path: Path,
) -> None:
    repository, source_sha = _create_repository(tmp_path)
    (repository / ".git" / "info" / "attributes").write_text(
        "tracked.txt export-ignore\n",
        encoding="utf-8",
    )
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"
    assert "tracked.txt" in payload["files"]


def test_repository_info_attributes_cannot_export_substitute_approved_bytes(
    tmp_path: Path,
) -> None:
    repository, _ = _create_repository(tmp_path)
    approved_bytes = "approved $Format:%H$ bytes\n"
    (repository / "tracked.txt").write_text(approved_bytes, encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--message", "approved archive marker")
    source_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", APPROVED_REF, source_sha)
    (repository / ".git" / "info" / "attributes").write_text(
        "tracked.txt export-subst\n",
        encoding="utf-8",
    )
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == approved_bytes


@pytest.mark.parametrize("attribute_source", ["xdg", "global-config"])
def test_global_attributes_cannot_change_the_approved_archive(
    tmp_path: Path,
    attribute_source: str,
) -> None:
    repository, source_sha = _create_repository(tmp_path)
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    (xdg / "git").mkdir(parents=True)
    if attribute_source == "xdg":
        (xdg / "git" / "attributes").write_text(
            "tracked.txt export-ignore\n",
            encoding="utf-8",
        )
    else:
        attributes = tmp_path / "configured-global-attributes"
        attributes.write_text("tracked.txt export-ignore\n", encoding="utf-8")
        (home / ".gitconfig").write_text(
            f"[core]\n\tattributesFile = {attributes}\n",
            encoding="utf-8",
        )
    capture = tmp_path / "capture.json"
    environment = _tool_environment(
        tmp_path,
        source_sha,
        capture,
        HOME=str(home),
        XDG_CONFIG_HOME=str(xdg),
    )

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"
    assert "tracked.txt" in payload["files"]


def test_committed_attributes_remain_approved_archive_policy(tmp_path: Path) -> None:
    repository, _ = _create_repository(tmp_path)
    (repository / "tracked.txt").write_text(
        "approved $Format:%H$ bytes\n",
        encoding="utf-8",
    )
    (repository / "policy-ignored.txt").write_text(
        "excluded by approved policy\n",
        encoding="utf-8",
    )
    (repository / ".gitattributes").write_text(
        "tracked.txt export-subst\npolicy-ignored.txt export-ignore\n",
        encoding="utf-8",
    )
    _git(
        repository,
        "add",
        ".gitattributes",
        "policy-ignored.txt",
        "tracked.txt",
    )
    _git(repository, "commit", "--message", "approved archive policy")
    source_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", APPROVED_REF, source_sha)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == f"approved {source_sha} bytes\n"
    assert ".gitattributes" in payload["files"]
    assert "policy-ignored.txt" not in payload["files"]


def test_isolated_archive_supports_a_linked_worktree(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    linked_worktree = tmp_path / "linked-worktree"
    _git(
        repository,
        "worktree",
        "add",
        "--detach",
        str(linked_worktree),
        source_sha,
    )
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(linked_worktree, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"


def test_isolated_archive_supports_an_alternate_object_database(tmp_path: Path) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    source, source_sha = _create_repository(source_parent)
    repository = tmp_path / "shared-clone"
    clone = _run(
        "git",
        "clone",
        "--shared",
        str(source),
        str(repository),
        cwd=tmp_path,
    )
    assert clone.returncode == 0, clone.stdout + clone.stderr
    _git(repository, "update-ref", APPROVED_REF, source_sha)
    assert (repository / ".git" / "objects" / "info" / "alternates").is_file()
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"


def test_isolated_archive_preserves_gitlink_archive_semantics(tmp_path: Path) -> None:
    repository, _ = _create_repository(tmp_path)
    submodule = tmp_path / "submodule"
    submodule.mkdir()
    _git(submodule, "init", "--initial-branch=main")
    _git(submodule, "config", "user.name", "Build Test")
    _git(
        submodule,
        "config",
        "user.email",
        "build-test@users.noreply.github.com",
    )
    (submodule / "module.txt").write_text("module bytes\n", encoding="utf-8")
    _git(submodule, "add", "module.txt")
    _git(submodule, "commit", "--message", "submodule source")
    _git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(submodule),
        "vendor/module",
    )
    _git(repository, "commit", "--message", "approved gitlink")
    source_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", APPROVED_REF, source_sha)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"
    assert ".gitmodules" in payload["files"]
    assert "vendor/module/module.txt" not in payload["files"]


def test_repository_argument_cannot_be_redirected_by_git_environment(
    tmp_path: Path,
) -> None:
    repository, source_sha = _create_repository(tmp_path)
    unrelated_parent = tmp_path / "unrelated-parent"
    unrelated_parent.mkdir()
    unrelated, _ = _create_repository(unrelated_parent)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(
        tmp_path,
        source_sha,
        capture,
        GIT_DIR=str(unrelated / ".git"),
        GIT_WORK_TREE=str(unrelated),
        GIT_REPLACE_REF_BASE="refs/replace-audit/",
    )

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["tracked"] == "approved bytes\n"


def test_repository_argument_must_be_the_exact_git_toplevel(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    nested = repository / "nested"
    nested.mkdir()
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(nested, source_sha, environment)

    assert result.returncode == 1
    assert "Git repository top level" in result.stderr
    assert not capture.exists()


def test_untracked_worktree_files_never_enter_the_build_context(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    (repository / "local-secret.txt").write_text("do not archive\n", encoding="utf-8")
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert "local-secret.txt" not in payload["files"]
    assert ".git" not in payload["files"]


def test_source_sha_must_equal_the_approved_remote_ref(tmp_path: Path) -> None:
    repository, approved_sha = _create_repository(tmp_path)
    (repository / "tracked.txt").write_text("unapproved commit\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--message", "unapproved source")
    unapproved_sha = _git(repository, "rev-parse", "HEAD")
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, unapproved_sha, capture)

    result = _build(repository, unapproved_sha, environment)

    assert approved_sha != unapproved_sha
    assert result.returncode == 1
    assert "does not match the approved remote ref" in result.stderr
    assert not capture.exists()


def test_source_sha_must_name_a_commit_object_not_an_annotated_tag(
    tmp_path: Path,
) -> None:
    repository, _source_sha = _create_repository(tmp_path)
    _git(repository, "tag", "--annotate", "approved-tag", "--message", "approved")
    tag_object_sha = _git(repository, "rev-parse", "refs/tags/approved-tag")
    assert _git(repository, "cat-file", "-t", tag_object_sha) == "tag"
    _git(repository, "update-ref", APPROVED_REF, tag_object_sha)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, tag_object_sha, capture)

    result = _build(repository, tag_object_sha, environment)

    assert result.returncode == 1
    assert "must name a commit object" in result.stderr
    assert not capture.exists()


def test_only_a_full_remote_tracking_ref_is_accepted(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)

    result = _build(
        repository,
        source_sha,
        environment,
        approved_ref="refs/heads/main",
    )

    assert result.returncode == 1
    assert "refs/remotes/" in result.stderr
    assert not capture.exists()


@pytest.mark.parametrize("source_sha", ["abc123", "A" * 40, "f" * 39, "g" * 40])
def test_source_sha_must_be_one_full_lowercase_object_id(
    tmp_path: Path,
    source_sha: str,
) -> None:
    repository, approved_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, approved_sha, capture)

    result = _build(repository, source_sha, environment)

    assert result.returncode == 1
    assert "40-character lowercase Git SHA" in result.stderr
    assert not capture.exists()


def test_build_refuses_a_mismatched_oci_revision_label(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(
        tmp_path,
        source_sha,
        capture,
        FAKE_DOCKER_LABEL="f" * 40,
    )

    result = _build(repository, source_sha, environment)

    assert result.returncode == 1
    assert "OCI revision label does not match SOURCE_SHA" in result.stderr


def test_build_refuses_a_nonimmutable_image_id(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(
        tmp_path,
        source_sha,
        capture,
        FAKE_DOCKER_IMAGE_ID="commerce-ops-desk:latest",
    )

    result = _build(repository, source_sha, environment)

    assert result.returncode == 1
    assert "immutable image ID" in result.stderr


def test_build_writes_one_private_strict_identity_manifest(tmp_path: Path) -> None:
    repository, source_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    environment = _tool_environment(tmp_path, source_sha, capture)
    manifest_directory = tmp_path / "private-manifests"
    manifest_directory.mkdir(mode=0o700)
    manifest = manifest_directory / "verified-build.json"

    result = _build(
        repository,
        source_sha,
        environment,
        build_manifest=manifest,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "approved_remote_ref": APPROVED_REF,
        "deployment_assets": {
            "schema": 1,
            "sha256": {
                relative_path: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for relative_path, content in DEPLOYMENT_ASSET_CONTENTS.items()
            },
        },
        "docker_daemon_id": "local-daemon-id",
        "image_id": "sha256:" + "a" * 64,
        "image_reference": f"commerce-ops-desk:{source_sha}",
        "schema": 2,
        "source_sha": source_sha,
    }


def test_builder_docker_commands_use_the_fixed_local_daemon_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_build_tool()
    monkeypatch.setenv("PATH", f"{tmp_path}/attacker-bin")
    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "attacker")
    monkeypatch.setenv("COMPOSE_FILE", "/tmp/attacker.yaml")
    observed: dict[str, object] = {}

    def fake_run(
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        observed.update(arguments=arguments, cwd=cwd, environment=env)
        assert capture_output is True
        assert text is True
        assert check is False
        assert timeout == module.COMMAND_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(arguments, 0, "local-daemon\n", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    output = module.CommandRunner().query(
        [module.DOCKER_BINARY, "info", "--format", "{{json .ID}}"],
        cwd=tmp_path,
        failure_message="docker info failed",
    )

    assert output == "local-daemon"
    assert observed["arguments"] == [
        "/usr/bin/docker",
        "info",
        "--format",
        "{{json .ID}}",
    ]
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert environment["DOCKER_CONFIG"] == "/etc/docker"
    assert environment["PATH"] == "/usr/bin:/bin"
    assert "DOCKER_CONTEXT" not in environment
    assert not any(name.startswith("COMPOSE_") for name in environment)


def test_build_binds_label_and_id_from_one_json_image_inspection(
    tmp_path: Path,
) -> None:
    repository, source_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    inspect_capture = tmp_path / "inspect.jsonl"
    environment = _tool_environment(
        tmp_path,
        source_sha,
        capture,
        FAKE_DOCKER_INSPECT_CAPTURE=str(inspect_capture),
        FAKE_DOCKER_REQUIRE_JSON_INSPECT="1",
    )

    result = _build(repository, source_sha, environment)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in inspect_capture.read_text().splitlines()]
    assert calls == [["image", "inspect", f"commerce-ops-desk:{source_sha}"]]


def test_failed_build_cleans_temporary_content_without_echoing_output(
    tmp_path: Path,
) -> None:
    repository, source_sha = _create_repository(tmp_path)
    capture = tmp_path / "capture.json"
    canary = "TOP_SECRET_BUILD_CANARY"
    environment = _tool_environment(
        tmp_path,
        source_sha,
        capture,
        FAKE_DOCKER_BUILD_FAILURE="1",
        SECRET_CANARY=canary,
    )
    temporary_root = Path(environment["TMPDIR"])

    result = _build(repository, source_sha, environment)

    assert result.returncode == 1
    assert "docker build failed" in result.stderr
    assert canary not in result.stdout
    assert canary not in result.stderr
    assert list(temporary_root.iterdir()) == []


def test_workstation_docs_and_gates_use_the_verified_builder() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "build_verified_image.py" in runbook
    assert "local provenance control" in runbook
    assert "cryptographic signature" in runbook
    assert "temporary bare object view" in runbook
    assert ".git/info/attributes" in runbook
    assert "Versioned attributes" in runbook
    assert "deploy/workstation/build_verified_image.py" in makefile
    assert "test_verified_image_build.py" in workflow
