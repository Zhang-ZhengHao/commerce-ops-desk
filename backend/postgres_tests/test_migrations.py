"""Executable migration and constraint proof against a real PostgreSQL server."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from postgres_tests.harness import (
    TemporaryPostgresDatabase,
    redact_database_credentials,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"
BOOTSTRAP_IDEMPOTENCY_REVISION = "0003_bootstrap_idempotency"
ORDER_CASE_REVISION = "0004_order_case"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

CASE_INSERT = text(
    """
    INSERT INTO exception_cases (
        id, organization_id, order_id, source_event_id, rule_key, case_type,
        severity, status, assignee_membership_id, due_at, resolution_reason,
        resolved_at, version, created_at, updated_at
    ) VALUES (
        :id, :organization_id, :order_id, :source_event_id, :rule_key, :case_type,
        :severity, :status, :assignee_membership_id, :due_at, :resolution_reason,
        :resolved_at, :version, :created_at, :updated_at
    )
    """
)


def run_alembic(
    database: TemporaryPostgresDatabase,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    database_url = database.url.render_as_string(hide_password=False)
    environment = os.environ.copy()
    environment.update(
        {
            "COMMERCE_OPS_DATABASE_URL": database_url,
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
            *arguments,
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def assert_alembic_succeeded(
    result: subprocess.CompletedProcess[str],
    database: TemporaryPostgresDatabase,
) -> None:
    diagnostic = redact_database_credentials(
        result.stdout + result.stderr,
        database.url,
    )
    if result.returncode != 0:
        raise AssertionError(diagnostic) from None


def read_applied_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert isinstance(revision, str)
    return revision


def migrate_to_head(
    database: TemporaryPostgresDatabase,
    engine: Engine,
) -> None:
    result = run_alembic(database, "upgrade", "head")
    assert_alembic_succeeded(result, database)
    assert read_applied_revision(engine) == ORDER_CASE_REVISION


def named_constraints(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    rows = [
        *inspector.get_check_constraints(table),
        *inspector.get_unique_constraints(table),
        *inspector.get_foreign_keys(table),
    ]
    return {str(row["name"]) for row in rows if row.get("name") is not None}


def foreign_key_shapes(
    engine: Engine,
    table: str,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    return {
        (
            tuple(str(column) for column in row["constrained_columns"]),
            str(row["referred_table"]),
            tuple(str(column) for column in row["referred_columns"]),
        )
        for row in inspect(engine).get_foreign_keys(table)
    }


def unique_shapes(engine: Engine, table: str) -> set[tuple[str, ...]]:
    return {
        tuple(str(column) for column in row["column_names"])
        for row in inspect(engine).get_unique_constraints(table)
    }


def assert_constraint_rejects(
    engine: Engine,
    expected_constraint: str,
    statement: Any,
    parameters: Mapping[str, Any],
) -> None:
    with pytest.raises(IntegrityError) as captured, engine.begin() as connection:
        connection.execute(statement, parameters)

    diagnostic = getattr(captured.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == expected_constraint


def valid_case_values(
    *,
    case_id: str,
    organization_id: str = "org-a",
    order_id: str = "order-a",
    source_event_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": case_id,
        "organization_id": organization_id,
        "order_id": order_id,
        "source_event_id": source_event_id or f"event-{case_id}",
        "rule_key": "payment_failed",
        "case_type": "payment",
        "severity": "high",
        "status": "open",
        "assignee_membership_id": None,
        "due_at": NOW + timedelta(hours=2),
        "resolution_reason": None,
        "resolved_at": None,
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


def seed_two_tenants(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations (
                    id, name, is_demo, created_at, expires_at
                ) VALUES (
                    :id, :name, :is_demo, :created_at, :expires_at
                )
                """
            ),
            [
                {
                    "id": "org-a",
                    "name": "Tenant A",
                    "is_demo": True,
                    "created_at": NOW,
                    "expires_at": NOW + timedelta(hours=4),
                },
                {
                    "id": "org-b",
                    "name": "Tenant B",
                    "is_demo": True,
                    "created_at": NOW,
                    "expires_at": NOW + timedelta(hours=4),
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO users (
                    id, organization_id, display_name, created_at
                ) VALUES (
                    :id, :organization_id, :display_name, :created_at
                )
                """
            ),
            [
                {
                    "id": "user-a",
                    "organization_id": "org-a",
                    "display_name": "Manager A",
                    "created_at": NOW,
                },
                {
                    "id": "user-b",
                    "organization_id": "org-b",
                    "display_name": "Manager B",
                    "created_at": NOW,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO memberships (
                    id, organization_id, user_id, role, created_at
                ) VALUES (
                    :id, :organization_id, :user_id, :role, :created_at
                )
                """
            ),
            [
                {
                    "id": "membership-a",
                    "organization_id": "org-a",
                    "user_id": "user-a",
                    "role": "manager",
                    "created_at": NOW,
                },
                {
                    "id": "membership-b",
                    "organization_id": "org-b",
                    "user_id": "user-b",
                    "role": "manager",
                    "created_at": NOW,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO orders (
                    id, organization_id, external_order_id, order_number,
                    amount_minor, currency, payment_status, fulfillment_status,
                    created_at, updated_at
                ) VALUES (
                    :id, :organization_id, :external_order_id, :order_number,
                    :amount_minor, :currency, :payment_status, :fulfillment_status,
                    :created_at, :updated_at
                )
                """
            ),
            [
                {
                    "id": "order-a",
                    "organization_id": "org-a",
                    "external_order_id": "external-order-a",
                    "order_number": "DEMO-1001",
                    "amount_minor": 12900,
                    "currency": "USD",
                    "payment_status": "failed",
                    "fulfillment_status": "unfulfilled",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": "order-b",
                    "organization_id": "org-b",
                    "external_order_id": "external-order-b",
                    "order_number": "DEMO-2001",
                    "amount_minor": 15900,
                    "currency": "USD",
                    "payment_status": "failed",
                    "fulfillment_status": "unfulfilled",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )
        connection.execute(
            CASE_INSERT,
            valid_case_values(
                case_id="case-a",
                source_event_id="event-a",
            ),
        )


def test_fresh_upgrade_reaches_order_case_head(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    assert postgres_engine.dialect.name == "postgresql"

    result = run_alembic(postgres_database, "upgrade", "head")

    assert_alembic_succeeded(result, postgres_database)
    assert read_applied_revision(postgres_engine) == ORDER_CASE_REVISION
    inspector = inspect(postgres_engine)
    assert {
        "organizations",
        "users",
        "memberships",
        "sessions",
        "rate_limits",
        "bootstrap_receipts",
        "command_receipts",
        "orders",
        "exception_cases",
        "case_notes",
        "audit_events",
    } <= set(inspector.get_table_names())
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM organizations")) == 0
        assert connection.scalar(text("SELECT count(*) FROM exception_cases")) == 0


def test_second_upgrade_is_a_no_op(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    first = run_alembic(postgres_database, "upgrade", "head")
    second = run_alembic(postgres_database, "upgrade", "head")

    assert_alembic_succeeded(first, postgres_database)
    assert_alembic_succeeded(second, postgres_database)
    assert read_applied_revision(postgres_engine) == ORDER_CASE_REVISION
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM alembic_version")) == 1


def test_alembic_check_reports_no_model_drift(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    upgrade = run_alembic(postgres_database, "upgrade", "head")
    assert_alembic_succeeded(upgrade, postgres_database)

    check = run_alembic(postgres_database, "check")

    assert_alembic_succeeded(check, postgres_database)
    assert read_applied_revision(postgres_engine) == ORDER_CASE_REVISION


def test_order_case_revision_round_trips_on_postgresql(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    upgrade = run_alembic(postgres_database, "upgrade", ORDER_CASE_REVISION)
    assert_alembic_succeeded(upgrade, postgres_database)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations (
                    id, name, is_demo, created_at, expires_at
                ) VALUES (
                    :id, :name, :is_demo, :created_at, :expires_at
                )
                """
            ),
            {
                "id": "round-trip-organization",
                "name": "Round-trip workspace",
                "is_demo": True,
                "created_at": NOW,
                "expires_at": NOW + timedelta(hours=4),
            },
        )

    downgrade = run_alembic(
        postgres_database,
        "downgrade",
        BOOTSTRAP_IDEMPOTENCY_REVISION,
    )
    assert_alembic_succeeded(downgrade, postgres_database)
    assert read_applied_revision(postgres_engine) == BOOTSTRAP_IDEMPOTENCY_REVISION
    downgraded_inspector = inspect(postgres_engine)
    assert {
        "orders",
        "exception_cases",
        "case_notes",
        "audit_events",
    }.isdisjoint(downgraded_inspector.get_table_names())
    assert "case_note_count" not in {
        str(column["name"]) for column in downgraded_inspector.get_columns("organizations")
    }
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM organizations WHERE id = :id"),
                {"id": "round-trip-organization"},
            )
            == 1
        )

    reupgrade = run_alembic(postgres_database, "upgrade", ORDER_CASE_REVISION)
    assert_alembic_succeeded(reupgrade, postgres_database)
    assert read_applied_revision(postgres_engine) == ORDER_CASE_REVISION
    reupgraded_inspector = inspect(postgres_engine)
    assert {
        "orders",
        "exception_cases",
        "case_notes",
        "audit_events",
    } <= set(reupgraded_inspector.get_table_names())
    with postgres_engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT count(*) AS organization_count, min(case_note_count) AS note_count
                FROM organizations
                WHERE id = :id
                """
            ),
            {"id": "round-trip-organization"},
        ).one()
    assert row.organization_count == 1
    assert row.note_count == 0


def test_order_case_schema_exposes_named_tenant_constraints(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    migrate_to_head(postgres_database, postgres_engine)

    assert {
        "ck_orders_amount_minor_nonnegative",
        "uq_orders_organization_id_id",
        "uq_orders_organization_external_order",
    } <= named_constraints(postgres_engine, "orders")
    assert {
        "ck_exception_cases_rule_key",
        "ck_exception_cases_case_type",
        "ck_exception_cases_severity",
        "ck_exception_cases_status",
        "ck_exception_cases_rule_shape",
        "ck_exception_cases_lifecycle_fields",
        "ck_exception_cases_resolution_reason",
        "ck_exception_cases_positive_version",
        "fk_exception_cases_organization_order",
        "fk_exception_cases_organization_assignee",
        "uq_exception_cases_organization_id_id",
        "uq_exception_cases_organization_event_rule",
    } <= named_constraints(postgres_engine, "exception_cases")
    assert {
        "fk_case_notes_organization_case",
        "fk_case_notes_organization_author",
        "uq_case_notes_organization_id_id",
    } <= named_constraints(postgres_engine, "case_notes")
    assert {
        "ck_audit_events_positive_object_version",
        "fk_audit_events_organization_actor",
        "uq_audit_events_organization_action_key",
        "uq_audit_events_organization_object_version",
    } <= named_constraints(postgres_engine, "audit_events")

    assert (
        ("organization_id", "order_id"),
        "orders",
        ("organization_id", "id"),
    ) in foreign_key_shapes(postgres_engine, "exception_cases")
    assert (
        ("organization_id", "assignee_membership_id"),
        "memberships",
        ("organization_id", "id"),
    ) in foreign_key_shapes(postgres_engine, "exception_cases")
    assert (
        ("organization_id", "case_id"),
        "exception_cases",
        ("organization_id", "id"),
    ) in foreign_key_shapes(postgres_engine, "case_notes")
    assert (
        "organization_id",
        "source_event_id",
        "rule_key",
    ) in unique_shapes(postgres_engine, "exception_cases")


def test_composite_tenant_foreign_keys_reject_cross_tenant_rows(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    migrate_to_head(postgres_database, postgres_engine)
    seed_two_tenants(postgres_engine)

    assert_constraint_rejects(
        postgres_engine,
        "fk_exception_cases_organization_order",
        CASE_INSERT,
        valid_case_values(
            case_id="cross-order-case",
            organization_id="org-b",
            order_id="order-a",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_exception_cases_organization_assignee",
        CASE_INSERT,
        valid_case_values(
            case_id="cross-assignee-case",
            status="assigned",
            assignee_membership_id="membership-b",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_case_notes_organization_case",
        text(
            """
            INSERT INTO case_notes (
                id, organization_id, case_id, author_membership_id, body, created_at
            ) VALUES (
                :id, :organization_id, :case_id, :author_membership_id, :body, :created_at
            )
            """
        ),
        {
            "id": "cross-case-note",
            "organization_id": "org-b",
            "case_id": "case-a",
            "author_membership_id": "membership-b",
            "body": "Synthetic investigation note",
            "created_at": NOW,
        },
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_case_notes_organization_author",
        text(
            """
            INSERT INTO case_notes (
                id, organization_id, case_id, author_membership_id, body, created_at
            ) VALUES (
                :id, :organization_id, :case_id, :author_membership_id, :body, :created_at
            )
            """
        ),
        {
            "id": "cross-author-note",
            "organization_id": "org-a",
            "case_id": "case-a",
            "author_membership_id": "membership-b",
            "body": "Synthetic investigation note",
            "created_at": NOW,
        },
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_audit_events_organization_actor",
        text(
            """
            INSERT INTO audit_events (
                id, organization_id, actor_membership_id, action_key, action,
                object_type, object_id, object_version, changes_json, created_at
            ) VALUES (
                :id, :organization_id, :actor_membership_id, :action_key, :action,
                :object_type, :object_id, :object_version, :changes_json, :created_at
            )
            """
        ),
        {
            "id": "cross-actor-audit",
            "organization_id": "org-a",
            "actor_membership_id": "membership-b",
            "action_key": "constraint-proof:cross-actor",
            "action": "case.assigned",
            "object_type": "case",
            "object_id": "case-a",
            "object_version": 2,
            "changes_json": "{}",
            "created_at": NOW,
        },
    )

    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM organizations")) == 2
        assert connection.scalar(text("SELECT count(*) FROM exception_cases")) == 1
        assert connection.scalar(text("SELECT count(*) FROM case_notes")) == 0
        assert connection.scalar(text("SELECT count(*) FROM audit_events")) == 0


def test_case_shape_lifecycle_resolution_and_uniqueness_checks_are_enforced(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    migrate_to_head(postgres_database, postgres_engine)
    seed_two_tenants(postgres_engine)

    assert_constraint_rejects(
        postgres_engine,
        "ck_orders_amount_minor_nonnegative",
        text(
            """
            INSERT INTO orders (
                id, organization_id, external_order_id, order_number,
                amount_minor, currency, payment_status, fulfillment_status,
                created_at, updated_at
            ) VALUES (
                :id, :organization_id, :external_order_id, :order_number,
                :amount_minor, :currency, :payment_status, :fulfillment_status,
                :created_at, :updated_at
            )
            """
        ),
        {
            "id": "negative-order",
            "organization_id": "org-a",
            "external_order_id": "external-negative",
            "order_number": "DEMO-9998",
            "amount_minor": -1,
            "currency": "USD",
            "payment_status": "failed",
            "fulfillment_status": "unfulfilled",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    assert_constraint_rejects(
        postgres_engine,
        "ck_exception_cases_rule_shape",
        CASE_INSERT,
        valid_case_values(
            case_id="invalid-rule-shape",
            case_type="refund",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "ck_exception_cases_lifecycle_fields",
        CASE_INSERT,
        valid_case_values(
            case_id="invalid-lifecycle",
            status="assigned",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "ck_exception_cases_resolution_reason",
        CASE_INSERT,
        valid_case_values(
            case_id="invalid-resolution",
            status="resolved",
            assignee_membership_id="membership-a",
            resolution_reason="refund_approved",
            resolved_at=NOW,
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "ck_exception_cases_positive_version",
        CASE_INSERT,
        valid_case_values(
            case_id="invalid-version",
            version=0,
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "uq_exception_cases_organization_event_rule",
        CASE_INSERT,
        valid_case_values(
            case_id="duplicate-event-rule",
            source_event_id="event-a",
        ),
    )

    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM orders")) == 2
        assert connection.scalar(text("SELECT count(*) FROM exception_cases")) == 1
