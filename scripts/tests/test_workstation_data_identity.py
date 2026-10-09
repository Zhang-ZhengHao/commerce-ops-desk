"""Data-directory identity contracts for workstation deployments."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_TOOL = PRODUCT_ROOT / "deploy" / "workstation" / "deploy.py"
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def load_deploy_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "commerce_ops_workstation_data_identity", DEPLOY_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_data_directories(tmp_path: Path) -> tuple[Path, Path, Path]:
    app_root = tmp_path / "app"
    app_root.mkdir(mode=0o700)
    live = app_root / "data-live"
    candidate = app_root / "data-candidate-0123456789ab"
    live.mkdir(mode=0o700)
    candidate.mkdir(mode=0o700)
    return app_root, live, candidate


def test_data_validation_rejects_distinct_paths_with_one_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_deploy_tool()
    app_root, live, candidate = create_data_directories(tmp_path)
    shared_identity = (123, 456, os.geteuid(), os.getegid())
    monkeypatch.setattr(
        module,
        "_data_directory_identity",
        lambda *_args, **_kwargs: shared_identity,
        raising=False,
    )

    with pytest.raises(module.DeploymentError, match="same directory identity"):
        module.validate_data_directories(
            app_root=app_root,
            live_data_dir=live,
            candidate_data_dir=candidate,
            source_sha=VALID_SHA,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )


def test_recorded_directory_identity_rejects_same_path_after_inode_replacement(
    tmp_path: Path,
) -> None:
    module = load_deploy_tool()
    _app_root, _live, candidate = create_data_directories(tmp_path)
    recorded = module._data_directory_identity(candidate)
    parked = candidate.with_name("parked-candidate")
    candidate.rename(parked)
    candidate.mkdir(mode=0o700)

    with pytest.raises(module.DeploymentError, match="identity changed"):
        module._assert_data_directory_identity(
            candidate,
            recorded,
            label="candidate data directory",
        )


def test_container_data_identity_must_equal_the_recorded_host_mount() -> None:
    module = load_deploy_tool()
    recorded = (64_769, 55_451_026, 10_001, 10_001)

    module._assert_container_data_identity(
        "64769|55451026|10001|10001\n",
        recorded,
    )
    with pytest.raises(module.DeploymentError, match="container data directory"):
        module._assert_container_data_identity(
            "64769|55451027|10001|10001\n",
            recorded,
        )


@pytest.mark.parametrize(
    "raw_identity",
    [
        "",
        "64769|55451026|10001",
        "64769|55451026|10001|10001|extra",
        "-1|55451026|10001|10001",
        "64769|not-an-inode|10001|10001",
        "64769|55451026|0|0",
    ],
)
def test_container_data_identity_rejects_malformed_or_wrong_stat_output(
    raw_identity: str,
) -> None:
    module = load_deploy_tool()

    with pytest.raises(module.DeploymentError, match="container data directory"):
        module._assert_container_data_identity(
            raw_identity,
            (64_769, 55_451_026, 10_001, 10_001),
        )
