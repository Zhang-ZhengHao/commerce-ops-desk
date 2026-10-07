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
DEMO_IDENTITY_REVISION = "0002_demo_identity"
BOOTSTRAP_IDEMPOTENCY_REVISION = "0003_bootstrap_idempotency"
HEAD_REVISION = BOOTSTRAP_IDEMPOTENCY_REVISION
DEMO_IDENTITY_TABLES = {
    "organizations",
    "users",
    "memberships",
    "sessions",
    "rate_limits",
    "bootstrap_receipts",
    "command_receipts",
}


def run_upgrade(
    database_path: Path,
    *,
    working_directory: Path = BACKEND_ROOT,
    target: str = "head",
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
            target,
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


def read_table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def read_columns(database_path: Path, table: str) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row[1]): int(row[3]) for row in rows}


def read_foreign_keys(database_path: Path, table: str) -> set[tuple[str, str, str]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    return {(str(row[3]), str(row[2]), str(row[4])) for row in rows}


def read_unique_indexes(database_path: Path, table: str) -> set[tuple[str, ...]]:
    with sqlite3.connect(database_path) as connection:
        index_rows = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
        unique_indexes: set[tuple[str, ...]] = set()
        for index_row in index_rows:
            if not int(index_row[2]):
                continue
            index_name = str(index_row[1]).replace('"', '""')
            column_rows = connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            unique_indexes.add(tuple(str(row[2]) for row in column_rows))
    return unique_indexes


def read_create_table_sql(database_path: Path, table: str) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    assert row is not None
    return str(row[0]).lower()


def test_upgrade_head_migrates_a_fresh_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"

    result = run_upgrade(database_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert database_path.is_file()
    assert read_applied_revision(database_path) == HEAD_REVISION


def test_upgrade_head_is_repeatable_for_an_up_to_date_database(tmp_path: Path) -> None:
    database_path = tmp_path / "repeatable.sqlite3"

    first_result = run_upgrade(database_path)
    second_result = run_upgrade(database_path)

    assert first_result.returncode == 0, first_result.stdout + first_result.stderr
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr
    assert read_applied_revision(database_path) == HEAD_REVISION


def test_explicit_config_migrates_from_an_unrelated_working_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "arbitrary-cwd.sqlite3"
    unrelated_directory = tmp_path / "unrelated"
    unrelated_directory.mkdir()

    result = run_upgrade(database_path, working_directory=unrelated_directory)

    assert result.returncode == 0, result.stdout + result.stderr
    assert read_applied_revision(database_path) == HEAD_REVISION


def test_demo_identity_migration_upgrades_the_foundation_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "upgrade-from-foundation.sqlite3"

    foundation_result = run_upgrade(database_path, target=FOUNDATION_REVISION)
    identity_result = run_upgrade(database_path)

    assert foundation_result.returncode == 0, foundation_result.stdout + foundation_result.stderr
    assert identity_result.returncode == 0, identity_result.stdout + identity_result.stderr
    assert read_applied_revision(database_path) == HEAD_REVISION
    assert read_table_names(database_path) >= DEMO_IDENTITY_TABLES


def test_bootstrap_receipts_upgrade_an_already_stamped_identity_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade-applied-identity.sqlite3"
    identity_result = run_upgrade(database_path, target=DEMO_IDENTITY_REVISION)
    assert identity_result.returncode == 0, identity_result.stdout + identity_result.stderr

    # 0002 is immutable: the hosted database applied it before bootstrap
    # idempotency existed, so that table belongs in the next revision.
    assert read_applied_revision(database_path) == DEMO_IDENTITY_REVISION
    assert "bootstrap_receipts" not in read_table_names(database_path)

    head_result = run_upgrade(database_path)

    assert head_result.returncode == 0, head_result.stdout + head_result.stderr
    assert read_applied_revision(database_path) == BOOTSTRAP_IDEMPOTENCY_REVISION
    assert "bootstrap_receipts" in read_table_names(database_path)


def test_demo_identity_schema_keeps_role_and_tenant_truth_in_memberships(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "identity-schema.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    membership_columns = read_columns(database_path, "memberships")
    session_columns = read_columns(database_path, "sessions")

    assert {"id", "organization_id", "user_id", "role"} <= membership_columns.keys()
    assert membership_columns["organization_id"] == 1
    assert membership_columns["user_id"] == 1
    assert membership_columns["role"] == 1
    assert {
        "organization_id",
        "membership_id",
        "token_hash",
        "expires_at",
        "revoked_at",
    } <= (session_columns.keys())
    assert "role" not in session_columns
    assert all("csrf" not in column_name.lower() for column_name in session_columns)
    assert ("organization_id", "organizations", "id") in read_foreign_keys(
        database_path, "memberships"
    )
    assert ("user_id", "users", "id") in read_foreign_keys(database_path, "memberships")
    assert ("membership_id", "memberships", "id") in read_foreign_keys(database_path, "sessions")
    assert ("organization_id", "organizations", "id") in read_foreign_keys(
        database_path, "sessions"
    )
    assert ("organization_id", "user_id") in read_unique_indexes(database_path, "memberships")
    assert ("token_hash",) in read_unique_indexes(database_path, "sessions")
    membership_sql = read_create_table_sql(database_path, "memberships")
    assert "check" in membership_sql
    assert "manager" in membership_sql
    assert "agent" in membership_sql


def test_command_receipt_schema_enforces_the_idempotency_scope(tmp_path: Path) -> None:
    database_path = tmp_path / "command-receipts.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    columns = read_columns(database_path, "command_receipts")

    assert {
        "organization_id",
        "membership_id",
        "command_type",
        "idempotency_key",
        "payload_digest",
        "response_json",
        "response_status",
        "result_membership_id",
        "result_session_id",
    } <= columns.keys()
    assert columns["organization_id"] == 1
    assert columns["membership_id"] == 1
    assert columns["command_type"] == 1
    assert columns["idempotency_key"] == 1
    assert columns["payload_digest"] == 1
    assert ("membership_id", "memberships", "id") in read_foreign_keys(
        database_path, "command_receipts"
    )
    assert ("result_membership_id", "memberships", "id") in read_foreign_keys(
        database_path, "command_receipts"
    )
    assert ("result_session_id", "sessions", "id") in read_foreign_keys(
        database_path, "command_receipts"
    )
    assert (
        "membership_id",
        "command_type",
        "idempotency_key",
    ) in read_unique_indexes(database_path, "command_receipts")


def test_bootstrap_receipt_schema_is_source_scoped_and_secret_free(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bootstrap-receipts.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    columns = read_columns(database_path, "bootstrap_receipts")

    assert {
        "source_digest",
        "idempotency_key",
        "payload_digest",
        "organization_id",
        "result_session_id",
        "response_json",
    } <= columns.keys()
    assert all("cookie" not in column.lower() for column in columns)
    assert all("csrf" not in column.lower() for column in columns)
    assert all("address" not in column.lower() for column in columns)
    assert ("source_digest", "idempotency_key") in read_unique_indexes(
        database_path,
        "bootstrap_receipts",
    )
    assert ("result_session_id", "sessions", "id") in read_foreign_keys(
        database_path,
        "bootstrap_receipts",
    )
