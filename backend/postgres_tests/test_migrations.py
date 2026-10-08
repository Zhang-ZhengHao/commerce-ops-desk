"""Executable migration and constraint proof against a real PostgreSQL server."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

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
WEBHOOK_INBOX_REVISION = "0005_webhook_inbox"
HEAD_REVISION = WEBHOOK_INBOX_REVISION
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

WEBHOOK_EVENT_INSERT = text(
    """
    INSERT INTO webhook_events (
        id, organization_id, integration_id, external_event_id, event_type,
        payload_digest, occurred_at, order_id, case_id, received_at, processed_at
    ) VALUES (
        :id, :organization_id, :integration_id, :external_event_id, :event_type,
        :payload_digest, :occurred_at, :order_id, :case_id, :received_at, :processed_at
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
    assert read_applied_revision(engine) == HEAD_REVISION


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


def index_shapes(engine: Engine, table: str) -> set[tuple[str, ...]]:
    return {
        tuple(str(column) for column in row["column_names"])
        for row in inspect(engine).get_indexes(table)
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


def valid_webhook_event_values(
    *,
    event_id: str,
    organization_id: str = "org-a",
    integration_id: str = "11111111-1111-4111-8111-111111111111",
    external_event_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": event_id,
        "organization_id": organization_id,
        "integration_id": integration_id,
        "external_event_id": external_event_id or f"evt_{event_id.replace('-', '')[:12]}",
        "event_type": "payment.failed",
        "payload_digest": "a" * 64,
        "occurred_at": NOW,
        "order_id": "order-a",
        "case_id": "case-a",
        "received_at": NOW + timedelta(seconds=1),
        "processed_at": NOW + timedelta(seconds=2),
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
    assert read_applied_revision(postgres_engine) == HEAD_REVISION
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
        "webhook_integrations",
        "webhook_events",
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
    assert read_applied_revision(postgres_engine) == HEAD_REVISION
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
    assert read_applied_revision(postgres_engine) == HEAD_REVISION


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


def test_webhook_inbox_revision_backfills_and_round_trips_on_postgresql(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    previous = run_alembic(postgres_database, "upgrade", ORDER_CASE_REVISION)
    assert_alembic_succeeded(previous, postgres_database)
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
            [
                {
                    "id": "webhook-demo",
                    "name": "Webhook demo",
                    "is_demo": True,
                    "created_at": NOW,
                    "expires_at": NOW + timedelta(hours=4),
                },
                {
                    "id": "webhook-non-demo",
                    "name": "Webhook non-demo",
                    "is_demo": False,
                    "created_at": NOW,
                    "expires_at": NOW + timedelta(hours=4),
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
                    'webhook-preexisting-order', 'webhook-demo',
                    'synthetic-webhook-preexisting', 'DEMO-9002', 4200, 'USD',
                    'failed', 'unfulfilled', :created_at, :updated_at
                )
                """
            ),
            {"created_at": NOW, "updated_at": NOW},
        )

    upgrade = run_alembic(postgres_database, "upgrade", "head")
    repeat = run_alembic(postgres_database, "upgrade", "head")
    assert_alembic_succeeded(upgrade, postgres_database)
    assert_alembic_succeeded(repeat, postgres_database)
    assert read_applied_revision(postgres_engine) == HEAD_REVISION
    with postgres_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, organization_id, provider, key_version, enabled
                FROM webhook_integrations
                ORDER BY organization_id
                """
            )
        ).all()
        counters = connection.execute(
            text(
                """
                SELECT id, webhook_event_count
                FROM organizations
                ORDER BY id
                """
            )
        ).all()
        event_count = connection.scalar(text("SELECT count(*) FROM webhook_events"))
    assert [(row.organization_id, row.provider, row.key_version, row.enabled) for row in rows] == [
        ("webhook-demo", "synthetic", 1, True)
    ]
    parsed_integration_id = UUID(rows[0].id)
    assert str(parsed_integration_id) == rows[0].id
    assert parsed_integration_id.version == 4
    first_integration_id = rows[0].id
    assert [(row.id, row.webhook_event_count) for row in counters] == [
        ("webhook-demo", 0),
        ("webhook-non-demo", 0),
    ]
    assert event_count == 0

    downgrade = run_alembic(postgres_database, "downgrade", ORDER_CASE_REVISION)
    assert_alembic_succeeded(downgrade, postgres_database)
    assert read_applied_revision(postgres_engine) == ORDER_CASE_REVISION
    downgraded = inspect(postgres_engine)
    assert {"webhook_integrations", "webhook_events"}.isdisjoint(downgraded.get_table_names())
    assert "webhook_event_count" not in {
        str(column["name"]) for column in downgraded.get_columns("organizations")
    }
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM organizations WHERE id = 'webhook-demo'"))
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM orders WHERE id = 'webhook-preexisting-order'")
            )
            == 1
        )

    reupgrade = run_alembic(postgres_database, "upgrade", "head")
    assert_alembic_succeeded(reupgrade, postgres_database)
    assert read_applied_revision(postgres_engine) == HEAD_REVISION
    with postgres_engine.connect() as connection:
        second_integration_id = connection.scalar(
            text(
                """
                SELECT id FROM webhook_integrations
                WHERE organization_id = 'webhook-demo'
                """
            )
        )
        assert connection.scalar(text("SELECT count(*) FROM webhook_events")) == 0
    assert isinstance(second_integration_id, str)
    assert second_integration_id != first_integration_id


def test_webhook_schema_exposes_named_tenant_constraints(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    migrate_to_head(postgres_database, postgres_engine)

    assert {
        "ck_webhook_integrations_provider",
        "ck_webhook_integrations_positive_key_version",
        "fk_webhook_integrations_organization",
        "uq_webhook_integrations_organization_id_id",
        "uq_webhook_integrations_organization_provider",
    } <= named_constraints(postgres_engine, "webhook_integrations")
    assert {
        "ck_webhook_events_event_type",
        "ck_webhook_events_payload_digest",
        "ck_webhook_events_processing_state",
        "fk_webhook_events_organization",
        "fk_webhook_events_organization_integration",
        "fk_webhook_events_organization_order",
        "fk_webhook_events_organization_case",
        "uq_webhook_events_organization_integration_external_event",
    } <= named_constraints(postgres_engine, "webhook_events")
    assert (
        ("organization_id", "integration_id"),
        "webhook_integrations",
        ("organization_id", "id"),
    ) in foreign_key_shapes(postgres_engine, "webhook_events")
    assert (
        ("organization_id", "order_id"),
        "orders",
        ("organization_id", "id"),
    ) in foreign_key_shapes(postgres_engine, "webhook_events")
    assert (
        ("organization_id", "case_id"),
        "exception_cases",
        ("organization_id", "id"),
    ) in foreign_key_shapes(postgres_engine, "webhook_events")
    assert (
        "organization_id",
        "integration_id",
        "external_event_id",
    ) in unique_shapes(postgres_engine, "webhook_events")
    assert ("id", "enabled") in index_shapes(postgres_engine, "webhook_integrations")
    assert ("organization_id", "received_at") in index_shapes(
        postgres_engine,
        "webhook_events",
    )
    integration_columns = {
        str(column["name"])
        for column in inspect(postgres_engine).get_columns("webhook_integrations")
    }
    event_columns = {
        str(column["name"]) for column in inspect(postgres_engine).get_columns("webhook_events")
    }
    assert integration_columns == {
        "id",
        "organization_id",
        "provider",
        "key_version",
        "enabled",
        "created_at",
        "updated_at",
    }
    assert event_columns == {
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


def test_webhook_constraints_reject_invalid_and_cross_tenant_rows(
    postgres_database: TemporaryPostgresDatabase,
    postgres_engine: Engine,
) -> None:
    migrate_to_head(postgres_database, postgres_engine)
    seed_two_tenants(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            CASE_INSERT,
            valid_case_values(
                case_id="case-b",
                organization_id="org-b",
                order_id="order-b",
            ),
        )
        connection.execute(
            text(
                """
                INSERT INTO webhook_integrations (
                    id, organization_id, provider, key_version, enabled,
                    created_at, updated_at
                ) VALUES (
                    :id, :organization_id, 'synthetic', 1, true,
                    :created_at, :updated_at
                )
                """
            ),
            [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "organization_id": "org-a",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "organization_id": "org-b",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ],
        )
        connection.execute(
            WEBHOOK_EVENT_INSERT,
            valid_webhook_event_values(
                event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                external_event_id="evt_DUPLICATE01",
            ),
        )

    assert_constraint_rejects(
        postgres_engine,
        "ck_webhook_integrations_provider",
        text(
            """
            INSERT INTO webhook_integrations (
                id, organization_id, provider, key_version, enabled,
                created_at, updated_at
            ) VALUES (
                :id, 'org-a', 'live-provider', 1, true, :created_at, :updated_at
            )
            """
        ),
        {"id": "33333333-3333-4333-8333-333333333333", "created_at": NOW, "updated_at": NOW},
    )
    assert_constraint_rejects(
        postgres_engine,
        "ck_webhook_integrations_positive_key_version",
        text(
            """
            INSERT INTO webhook_integrations (
                id, organization_id, provider, key_version, enabled,
                created_at, updated_at
            ) VALUES (
                :id, 'org-a', 'synthetic', 0, true, :created_at, :updated_at
            )
            """
        ),
        {"id": "44444444-4444-4444-8444-444444444444", "created_at": NOW, "updated_at": NOW},
    )
    for event_id, invalid_digest in (
        ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "a" * 63),
        ("bcbcbcbc-bcbc-4bcb-8bcb-bcbcbcbcbcbc", "A" * 64),
        ("bdbdbdbd-bdbd-4bdb-8bdb-bdbdbdbdbdbd", "g" * 64),
        ("bebebebe-bebe-4beb-8beb-bebebebebebe", "-" * 64),
    ):
        assert_constraint_rejects(
            postgres_engine,
            "ck_webhook_events_payload_digest",
            WEBHOOK_EVENT_INSERT,
            valid_webhook_event_values(
                event_id=event_id,
                payload_digest=invalid_digest,
            ),
        )
    assert_constraint_rejects(
        postgres_engine,
        "ck_webhook_events_processing_state",
        WEBHOOK_EVENT_INSERT,
        valid_webhook_event_values(
            event_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            case_id=None,
            processed_at=None,
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_webhook_events_organization_integration",
        WEBHOOK_EVENT_INSERT,
        valid_webhook_event_values(
            event_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            integration_id="22222222-2222-4222-8222-222222222222",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_webhook_events_organization_order",
        WEBHOOK_EVENT_INSERT,
        valid_webhook_event_values(
            event_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            order_id="order-b",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "fk_webhook_events_organization_case",
        WEBHOOK_EVENT_INSERT,
        valid_webhook_event_values(
            event_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            case_id="case-b",
        ),
    )
    assert_constraint_rejects(
        postgres_engine,
        "uq_webhook_events_organization_integration_external_event",
        WEBHOOK_EVENT_INSERT,
        valid_webhook_event_values(
            event_id="99999999-9999-4999-8999-999999999999",
            external_event_id="evt_DUPLICATE01",
        ),
    )

    with postgres_engine.begin() as connection:
        connection.execute(
            WEBHOOK_EVENT_INSERT,
            valid_webhook_event_values(
                event_id="88888888-8888-4888-8888-888888888888",
                organization_id="org-b",
                integration_id="22222222-2222-4222-8222-222222222222",
                external_event_id="evt_DUPLICATE01",
                order_id=None,
                case_id=None,
                processed_at=None,
            ),
        )
        connection.execute(text("DELETE FROM organizations WHERE id = 'org-a'"))

    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM webhook_integrations WHERE organization_id = 'org-a'")
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM webhook_events WHERE organization_id = 'org-a'")
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM webhook_events WHERE organization_id = 'org-b'")
            )
            == 1
        )


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
