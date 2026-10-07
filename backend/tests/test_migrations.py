"""Executable migration contract for the supported SQLite demo database."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"
FOUNDATION_REVISION = "0001_foundation"


def run_upgrade(
    database_path: Path,
    *,
    working_directory: Path = BACKEND_ROOT,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "COMMERCE_OPS_DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "COMMERCE_OPS_ENVIRONMENT": "test",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ALEMBIC_CONFIG),
            "upgrade",
            "head",
        ],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def read_applied_revision(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert row is not None
    return str(row[0])


def test_upgrade_head_migrates_a_fresh_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"

    result = run_upgrade(database_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert database_path.is_file()
    assert read_applied_revision(database_path) == FOUNDATION_REVISION


def test_upgrade_head_is_repeatable_for_an_up_to_date_database(tmp_path: Path) -> None:
    database_path = tmp_path / "repeatable.sqlite3"

    first_result = run_upgrade(database_path)
    second_result = run_upgrade(database_path)

    assert first_result.returncode == 0, first_result.stdout + first_result.stderr
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr
    assert read_applied_revision(database_path) == FOUNDATION_REVISION


def test_explicit_config_migrates_from_an_unrelated_working_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "arbitrary-cwd.sqlite3"
    unrelated_directory = tmp_path / "unrelated"
    unrelated_directory.mkdir()

    result = run_upgrade(database_path, working_directory=unrelated_directory)

    assert result.returncode == 0, result.stdout + result.stderr
    assert read_applied_revision(database_path) == FOUNDATION_REVISION
