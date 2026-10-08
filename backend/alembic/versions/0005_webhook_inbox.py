"""Add demo webhook integrations and the tenant-scoped inbox.

Revision ID: 0005_webhook_inbox
Revises: 0004_order_case
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import context, op

revision: str = "0005_webhook_inbox"
down_revision: str | Sequence[str] | None = "0004_order_case"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYLOAD_DIGEST_CHECK = (
    "length(payload_digest) = 64 "
    "AND payload_digest = lower(payload_digest) "
    "AND length("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "payload_digest, '0', ''), '1', ''), '2', ''), '3', ''), "
    "'4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), "
    "'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')"
    ") = 0"
)
POSTGRESQL_OFFLINE_BACKFILL_GUARD = """
DO $commerce_ops$
BEGIN
    IF EXISTS (
        SELECT 1 FROM organizations WHERE is_demo IS TRUE
    ) THEN
        RAISE EXCEPTION
            '0005_webhook_inbox requires an online migration when demo organizations already exist';
    END IF;
END
$commerce_ops$
"""


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "webhook_event_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.create_table(
        "webhook_integrations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider = 'synthetic'",
            name="ck_webhook_integrations_provider",
        ),
        sa.CheckConstraint(
            "key_version >= 1",
            name="ck_webhook_integrations_positive_key_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_webhook_integrations_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_webhook_integrations_organization_id_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_webhook_integrations_organization_provider",
        ),
    )
    op.create_index(
        "ix_webhook_integrations_id_enabled",
        "webhook_integrations",
        ["id", "enabled"],
        unique=False,
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("integration_id", sa.String(length=36), nullable=False),
        sa.Column("external_event_id", sa.String(length=68), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type = 'payment.failed'",
            name="ck_webhook_events_event_type",
        ),
        sa.CheckConstraint(
            PAYLOAD_DIGEST_CHECK,
            name="ck_webhook_events_payload_digest",
        ),
        sa.CheckConstraint(
            "(order_id IS NULL AND case_id IS NULL AND processed_at IS NULL) "
            "OR (order_id IS NOT NULL AND case_id IS NOT NULL "
            "AND processed_at IS NOT NULL)",
            name="ck_webhook_events_processing_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_webhook_events_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["webhook_integrations.organization_id", "webhook_integrations.id"],
            name="fk_webhook_events_organization_integration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_webhook_events_organization_order",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "case_id"],
            ["exception_cases.organization_id", "exception_cases.id"],
            name="fk_webhook_events_organization_case",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "integration_id",
            "external_event_id",
            name="uq_webhook_events_organization_integration_external_event",
        ),
    )
    op.create_index(
        "ix_webhook_events_organization_received_at",
        "webhook_events",
        ["organization_id", "received_at"],
        unique=False,
    )

    organizations = sa.table(
        "organizations",
        sa.column("id", sa.String(length=36)),
        sa.column("is_demo", sa.Boolean()),
    )
    integrations = sa.table(
        "webhook_integrations",
        sa.column("id", sa.String(length=36)),
        sa.column("organization_id", sa.String(length=36)),
        sa.column("provider", sa.String(length=32)),
        sa.column("key_version", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    if context.is_offline_mode():
        op.execute(sa.text(POSTGRESQL_OFFLINE_BACKFILL_GUARD))
        return

    demo_organization_ids = connection.scalars(
        sa.select(organizations.c.id).where(organizations.c.is_demo.is_(True))
    ).all()
    now = datetime.now(UTC)
    if demo_organization_ids:
        op.bulk_insert(
            integrations,
            [
                {
                    "id": str(uuid4()),
                    "organization_id": organization_id,
                    "provider": "synthetic",
                    "key_version": 1,
                    "enabled": True,
                    "created_at": now,
                    "updated_at": now,
                }
                for organization_id in demo_organization_ids
            ],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_events_organization_received_at",
        table_name="webhook_events",
    )
    op.drop_table("webhook_events")
    op.drop_index(
        "ix_webhook_integrations_id_enabled",
        table_name="webhook_integrations",
    )
    op.drop_table("webhook_integrations")
    op.drop_column("organizations", "webhook_event_count")
