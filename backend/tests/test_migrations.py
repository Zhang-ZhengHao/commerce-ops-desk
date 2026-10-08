"""Executable migration contract for the supported SQLite demo database."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"
FOUNDATION_REVISION = "0001_foundation"
DEMO_IDENTITY_REVISION = "0002_demo_identity"
BOOTSTRAP_IDEMPOTENCY_REVISION = "0003_bootstrap_idempotency"
ORDER_CASE_REVISION = "0004_order_case"
WEBHOOK_INBOX_REVISION = "0005_webhook_inbox"
HEAD_REVISION = WEBHOOK_INBOX_REVISION
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


def run_downgrade(
    database_path: Path,
    *,
    target: str,
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
            "downgrade",
            target,
        ],
        cwd=BACKEND_ROOT,
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


def read_foreign_key_shapes(
    database_path: Path,
    table: str,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()

    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(row)

    return {
        (
            tuple(str(row[3]) for row in sorted(group, key=lambda row: int(row[1]))),
            str(group[0][2]),
            tuple(str(row[4]) for row in sorted(group, key=lambda row: int(row[1]))),
            str(group[0][6]).upper(),
        )
        for group in grouped.values()
    }


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


def read_index_shapes(database_path: Path, table: str) -> set[tuple[str, ...]]:
    with sqlite3.connect(database_path) as connection:
        index_rows = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
        indexes: set[tuple[str, ...]] = set()
        for index_row in index_rows:
            index_name = str(index_row[1]).replace('"', '""')
            column_rows = connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            indexes.add(tuple(str(row[2]) for row in column_rows))
    return indexes


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
    assert read_applied_revision(database_path) == HEAD_REVISION
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


def test_order_case_migration_adds_tenant_scoped_workflow_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "order-case-schema.sqlite3"

    previous_result = run_upgrade(database_path, target=BOOTSTRAP_IDEMPOTENCY_REVISION)
    assert previous_result.returncode == 0, previous_result.stdout + previous_result.stderr
    assert {
        "orders",
        "exception_cases",
        "case_notes",
        "audit_events",
    }.isdisjoint(read_table_names(database_path))
    assert "case_note_count" not in read_columns(database_path, "organizations")

    head_result = run_upgrade(database_path, target=ORDER_CASE_REVISION)

    assert head_result.returncode == 0, head_result.stdout + head_result.stderr
    assert read_applied_revision(database_path) == ORDER_CASE_REVISION
    assert {
        "orders",
        "exception_cases",
        "case_notes",
        "audit_events",
    } <= read_table_names(database_path)
    for table in ("orders", "exception_cases", "case_notes", "audit_events"):
        assert read_columns(database_path, table)["organization_id"] == 1
    assert read_columns(database_path, "organizations")["case_note_count"] == 1

    case_columns = read_columns(database_path, "exception_cases")
    assert {
        "order_id",
        "source_event_id",
        "rule_key",
        "case_type",
        "severity",
        "status",
        "assignee_membership_id",
        "due_at",
        "resolution_reason",
        "resolved_at",
        "version",
    } <= case_columns.keys()
    assert ("organization_id", "source_event_id", "rule_key") in read_unique_indexes(
        database_path,
        "exception_cases",
    )
    assert ("organization_id", "action_key") in read_unique_indexes(
        database_path,
        "audit_events",
    )
    case_sql = read_create_table_sql(database_path, "exception_cases")
    assert "payment_failed" in case_sql
    assert "refund_review" in case_sql
    assert "fulfillment_delayed" in case_sql
    assert "open" in case_sql
    assert "assigned" in case_sql
    assert "resolved" in case_sql


def test_order_case_migration_downgrades_to_the_previous_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "order-case-downgrade.sqlite3"
    head_result = run_upgrade(database_path, target=ORDER_CASE_REVISION)
    assert head_result.returncode == 0, head_result.stdout + head_result.stderr

    downgrade_result = run_downgrade(database_path, target=BOOTSTRAP_IDEMPOTENCY_REVISION)

    assert downgrade_result.returncode == 0, downgrade_result.stdout + downgrade_result.stderr
    assert read_applied_revision(database_path) == BOOTSTRAP_IDEMPOTENCY_REVISION
    assert {
        "orders",
        "exception_cases",
        "case_notes",
        "audit_events",
    }.isdisjoint(read_table_names(database_path))
    assert "case_note_count" not in read_columns(database_path, "organizations")


def _insert_migration_organization(
    connection: sqlite3.Connection,
    *,
    organization_id: str,
    is_demo: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO organizations (
            id, name, is_demo, case_note_count, created_at, expires_at
        ) VALUES (?, ?, ?, 0, '2026-10-08 12:00:00', '2026-10-08 16:00:00')
        """,
        (organization_id, f"Workspace {organization_id}", is_demo),
    )


def test_webhook_inbox_upgrade_backfills_one_integration_per_demo_organization(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "webhook-backfill.sqlite3"
    previous_result = run_upgrade(database_path, target=ORDER_CASE_REVISION)
    assert previous_result.returncode == 0, previous_result.stdout + previous_result.stderr

    with sqlite3.connect(database_path) as connection:
        _insert_migration_organization(
            connection,
            organization_id="demo-a",
            is_demo=True,
        )
        _insert_migration_organization(
            connection,
            organization_id="demo-b",
            is_demo=True,
        )
        _insert_migration_organization(
            connection,
            organization_id="non-demo",
            is_demo=False,
        )

    upgrade_result = run_upgrade(database_path)
    repeat_result = run_upgrade(database_path)

    assert upgrade_result.returncode == 0, upgrade_result.stdout + upgrade_result.stderr
    assert repeat_result.returncode == 0, repeat_result.stdout + repeat_result.stderr
    assert read_applied_revision(database_path) == WEBHOOK_INBOX_REVISION
    with sqlite3.connect(database_path) as connection:
        integrations = connection.execute(
            """
            SELECT id, organization_id, provider, key_version, enabled
            FROM webhook_integrations
            ORDER BY organization_id
            """
        ).fetchall()
        counters = connection.execute(
            """
            SELECT id, webhook_event_count
            FROM organizations
            ORDER BY id
            """
        ).fetchall()
        event_count = connection.execute("SELECT count(*) FROM webhook_events").fetchone()

    assert [(row[1], row[2], row[3], row[4]) for row in integrations] == [
        ("demo-a", "synthetic", 1, 1),
        ("demo-b", "synthetic", 1, 1),
    ]
    parsed_integration_ids = [UUID(str(row[0])) for row in integrations]
    assert all(
        str(parsed) == row[0]
        for parsed, row in zip(parsed_integration_ids, integrations, strict=True)
    )
    assert all(parsed.version == 4 for parsed in parsed_integration_ids)
    assert counters == [("demo-a", 0), ("demo-b", 0), ("non-demo", 0)]
    assert event_count == (0,)


def test_webhook_inbox_schema_is_tenant_scoped_and_secret_free(tmp_path: Path) -> None:
    database_path = tmp_path / "webhook-schema.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    integration_columns = read_columns(database_path, "webhook_integrations")
    event_columns = read_columns(database_path, "webhook_events")
    assert set(integration_columns) == {
        "id",
        "organization_id",
        "provider",
        "key_version",
        "enabled",
        "created_at",
        "updated_at",
    }
    assert set(event_columns) == {
        "id",
        "organization_id",
        "integration_id",
        "external_event_id",
        "event_type",
        "payload_digest",
        "occurred_at",
        "order_id",
        "case_id",
        "received_at",
        "processed_at",
    }
    assert ("organization_id", "id") in read_unique_indexes(
        database_path,
        "webhook_integrations",
    )
    assert ("organization_id", "provider") in read_unique_indexes(
        database_path,
        "webhook_integrations",
    )
    assert (
        "organization_id",
        "integration_id",
        "external_event_id",
    ) in read_unique_indexes(database_path, "webhook_events")
    assert (
        ("organization_id", "integration_id"),
        "webhook_integrations",
        ("organization_id", "id"),
        "CASCADE",
    ) in read_foreign_key_shapes(database_path, "webhook_events")
    assert (
        ("organization_id", "order_id"),
        "orders",
        ("organization_id", "id"),
        "NO ACTION",
    ) in read_foreign_key_shapes(database_path, "webhook_events")
    assert (
        ("organization_id", "case_id"),
        "exception_cases",
        ("organization_id", "id"),
        "NO ACTION",
    ) in read_foreign_key_shapes(database_path, "webhook_events")
    assert ("id", "enabled") in read_index_shapes(
        database_path,
        "webhook_integrations",
    )
    assert ("organization_id", "received_at") in read_index_shapes(
        database_path,
        "webhook_events",
    )
    event_sql = read_create_table_sql(database_path, "webhook_events")
    assert "payment.failed" in event_sql
    assert "payload_digest" in event_sql
    assert "processed_at" in event_sql
    assert read_columns(database_path, "organizations")["webhook_event_count"] == 1


def test_webhook_inbox_revision_round_trips_without_losing_pre_i04_records(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "webhook-round-trip.sqlite3"
    previous_result = run_upgrade(database_path, target=ORDER_CASE_REVISION)
    assert previous_result.returncode == 0, previous_result.stdout + previous_result.stderr

    with sqlite3.connect(database_path) as connection:
        _insert_migration_organization(
            connection,
            organization_id="round-trip-demo",
            is_demo=True,
        )
        connection.execute(
            """
            INSERT INTO orders (
                id, organization_id, external_order_id, order_number,
                amount_minor, currency, payment_status, fulfillment_status,
                created_at, updated_at
            ) VALUES (
                'round-trip-order', 'round-trip-demo', 'synthetic-round-trip',
                'DEMO-9001', 2500, 'USD', 'failed', 'unfulfilled',
                '2026-10-08 12:00:00', '2026-10-08 12:00:00'
            )
            """
        )

    upgrade_result = run_upgrade(database_path)
    assert upgrade_result.returncode == 0, upgrade_result.stdout + upgrade_result.stderr
    with sqlite3.connect(database_path) as connection:
        first_integration_id = connection.execute(
            """
            SELECT id FROM webhook_integrations
            WHERE organization_id = 'round-trip-demo'
            """
        ).fetchone()
    assert first_integration_id is not None

    downgrade_result = run_downgrade(database_path, target=ORDER_CASE_REVISION)
    assert downgrade_result.returncode == 0, downgrade_result.stdout + downgrade_result.stderr
    assert read_applied_revision(database_path) == ORDER_CASE_REVISION
    assert {"webhook_integrations", "webhook_events"}.isdisjoint(read_table_names(database_path))
    assert "webhook_event_count" not in read_columns(database_path, "organizations")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM organizations WHERE id = 'round-trip-demo'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM orders WHERE id = 'round-trip-order'"
        ).fetchone() == (1,)

    reupgrade_result = run_upgrade(database_path)
    assert reupgrade_result.returncode == 0, reupgrade_result.stdout + reupgrade_result.stderr
    with sqlite3.connect(database_path) as connection:
        second_integration_id = connection.execute(
            """
            SELECT id FROM webhook_integrations
            WHERE organization_id = 'round-trip-demo'
            """
        ).fetchone()
        assert connection.execute("SELECT count(*) FROM webhook_events").fetchone() == (0,)
    assert second_integration_id is not None
    assert second_integration_id != first_integration_id


def _seed_case_constraint_parents(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        INSERT INTO organizations (
            id, name, is_demo, case_note_count, created_at, expires_at
        ) VALUES (
            'organization-1', 'Constraint workspace', 1, 0,
            '2026-10-07 12:00:00', '2026-10-07 16:00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO users (id, organization_id, display_name, created_at)
        VALUES (
            'user-1', 'organization-1', 'Constraint Agent',
            '2026-10-07 12:00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO memberships (id, organization_id, user_id, role, created_at)
        VALUES (
            'membership-1', 'organization-1', 'user-1', 'agent',
            '2026-10-07 12:00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO orders (
            id, organization_id, external_order_id, order_number,
            amount_minor, currency, payment_status, fulfillment_status,
            created_at, updated_at
        ) VALUES (
            'order-1', 'organization-1', 'external-order-1', 'DEMO-CONSTRAINT',
            1000, 'USD', 'failed', 'unfulfilled',
            '2026-10-07 12:00:00', '2026-10-07 12:00:00'
        )
        """
    )


def _insert_constraint_case(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    **overrides: Any,
) -> None:
    values: dict[str, Any] = {
        "id": case_id,
        "organization_id": "organization-1",
        "order_id": "order-1",
        "source_event_id": f"event-{case_id}",
        "rule_key": "payment_failed",
        "case_type": "payment",
        "severity": "high",
        "status": "open",
        "assignee_membership_id": None,
        "due_at": "2026-10-07 14:00:00",
        "resolution_reason": None,
        "resolved_at": None,
        "version": 1,
        "created_at": "2026-10-07 12:00:00",
        "updated_at": "2026-10-07 12:00:00",
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO exception_cases (
            id, organization_id, order_id, source_event_id, rule_key,
            case_type, severity, status, assignee_membership_id, due_at,
            resolution_reason, resolved_at, version, created_at, updated_at
        ) VALUES (
            :id, :organization_id, :order_id, :source_event_id, :rule_key,
            :case_type, :severity, :status, :assignee_membership_id, :due_at,
            :resolution_reason, :resolved_at, :version, :created_at, :updated_at
        )
        """,
        values,
    )


def _insert_webhook_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    **overrides: Any,
) -> None:
    values: dict[str, Any] = {
        "id": event_id,
        "organization_id": "organization-1",
        "integration_id": "11111111-1111-4111-8111-111111111111",
        "external_event_id": f"evt_{event_id.replace('-', '')[:12]}",
        "event_type": "payment.failed",
        "payload_digest": "a" * 64,
        "occurred_at": "2026-10-08 12:00:00",
        "order_id": "order-1",
        "case_id": "case-1",
        "received_at": "2026-10-08 12:00:01",
        "processed_at": "2026-10-08 12:00:02",
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO webhook_events (
            id, organization_id, integration_id, external_event_id,
            event_type, payload_digest, occurred_at, order_id, case_id,
            received_at, processed_at
        ) VALUES (
            :id, :organization_id, :integration_id, :external_event_id,
            :event_type, :payload_digest, :occurred_at, :order_id, :case_id,
            :received_at, :processed_at
        )
        """,
        values,
    )


def test_webhook_event_constraints_reject_partial_or_cross_tenant_records(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "webhook-event-constraints.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _seed_case_constraint_parents(connection)
        _insert_constraint_case(connection, case_id="case-1")
        _insert_migration_organization(
            connection,
            organization_id="organization-2",
            is_demo=True,
        )
        connection.execute(
            """
            INSERT INTO orders (
                id, organization_id, external_order_id, order_number,
                amount_minor, currency, payment_status, fulfillment_status,
                created_at, updated_at
            ) VALUES (
                'order-2', 'organization-2', 'external-order-2', 'DEMO-2002',
                2000, 'USD', 'failed', 'unfulfilled',
                '2026-10-08 12:00:00', '2026-10-08 12:00:00'
            )
            """
        )
        _insert_constraint_case(
            connection,
            case_id="case-2",
            organization_id="organization-2",
            order_id="order-2",
        )
        connection.executemany(
            """
            INSERT INTO webhook_integrations (
                id, organization_id, provider, key_version, enabled,
                created_at, updated_at
            ) VALUES (?, ?, 'synthetic', 1, 1, ?, ?)
            """,
            [
                (
                    "11111111-1111-4111-8111-111111111111",
                    "organization-1",
                    "2026-10-08 12:00:00",
                    "2026-10-08 12:00:00",
                ),
                (
                    "22222222-2222-4222-8222-222222222222",
                    "organization-2",
                    "2026-10-08 12:00:00",
                    "2026-10-08 12:00:00",
                ),
            ],
        )

        _insert_webhook_event(
            connection,
            event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            external_event_id="evt_DUPLICATE01",
        )

        for suffix, invalid_digest in (
            ("short", "a" * 63),
            ("uppercase", "A" * 64),
            ("nonhex", "g" * 64),
            ("punctuation", "-" * 64),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_webhook_event(
                    connection,
                    event_id=f"digest-{suffix}",
                    external_event_id=f"evt_DIGEST{suffix.upper()}",
                    payload_digest=invalid_digest,
                )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_webhook_event(
                connection,
                event_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                external_event_id="evt_PARTIAL01",
                order_id="order-1",
                case_id=None,
                processed_at=None,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_webhook_event(
                connection,
                event_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                external_event_id="evt_CROSSTENANT1",
                integration_id="22222222-2222-4222-8222-222222222222",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_webhook_event(
                connection,
                event_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                external_event_id="evt_CROSSORDER1",
                order_id="order-2",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_webhook_event(
                connection,
                event_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                external_event_id="evt_CROSSCASE01",
                case_id="case-2",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_webhook_event(
                connection,
                event_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
                external_event_id="evt_WRONGTYPE01",
                event_type="payment.succeeded",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_webhook_event(
                connection,
                event_id="99999999-9999-4999-8999-999999999999",
                external_event_id="evt_DUPLICATE01",
            )

        _insert_webhook_event(
            connection,
            event_id="88888888-8888-4888-8888-888888888888",
            organization_id="organization-2",
            integration_id="22222222-2222-4222-8222-222222222222",
            external_event_id="evt_DUPLICATE01",
            order_id=None,
            case_id=None,
            processed_at=None,
        )
        connection.execute("DELETE FROM organizations WHERE id = 'organization-1'")

        assert connection.execute(
            "SELECT count(*) FROM webhook_integrations WHERE organization_id = 'organization-1'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM webhook_events WHERE organization_id = 'organization-1'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM webhook_events WHERE organization_id = 'organization-2'"
        ).fetchone() == (1,)


def test_case_schema_requires_a_source_event_for_idempotency(tmp_path: Path) -> None:
    database_path = tmp_path / "case-source-event-constraint.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        _seed_case_constraint_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_constraint_case(
                connection,
                case_id="case-without-source",
                source_event_id=None,
            )


@pytest.mark.parametrize(
    ("rule_key", "case_type", "severity"),
    [
        ("payment_failed", "refund", "high"),
        ("refund_review", "refund", "high"),
        ("fulfillment_delayed", "payment", "medium"),
    ],
)
def test_case_schema_rejects_rule_shape_mismatches(
    tmp_path: Path,
    rule_key: str,
    case_type: str,
    severity: str,
) -> None:
    database_path = tmp_path / f"case-rule-shape-{rule_key}.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        _seed_case_constraint_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_constraint_case(
                connection,
                case_id=f"case-{rule_key}",
                rule_key=rule_key,
                case_type=case_type,
                severity=severity,
            )


@pytest.mark.parametrize(
    ("case_id", "overrides"),
    [
        ("assigned-without-agent", {"status": "assigned"}),
        (
            "open-with-agent",
            {"status": "open", "assignee_membership_id": "membership-1"},
        ),
        (
            "open-with-resolution",
            {
                "status": "open",
                "resolution_reason": "payment_recovered",
                "resolved_at": "2026-10-07 12:30:00",
            },
        ),
        (
            "resolved-without-result",
            {"status": "resolved", "version": 2},
        ),
    ],
)
def test_case_schema_rejects_incoherent_lifecycle_fields(
    tmp_path: Path,
    case_id: str,
    overrides: dict[str, Any],
) -> None:
    database_path = tmp_path / f"case-lifecycle-{case_id}.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        _seed_case_constraint_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_constraint_case(connection, case_id=case_id, **overrides)


def test_case_schema_rejects_a_resolution_for_another_rule(tmp_path: Path) -> None:
    database_path = tmp_path / "case-resolution-rule-constraint.sqlite3"
    result = run_upgrade(database_path)
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        _seed_case_constraint_parents(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_constraint_case(
                connection,
                case_id="case-invalid-resolution",
                status="resolved",
                resolution_reason="refund_approved",
                resolved_at="2026-10-07 12:30:00",
                version=2,
            )
