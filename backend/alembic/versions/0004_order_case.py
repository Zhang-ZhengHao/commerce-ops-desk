"""Add the tenant-scoped order and exception-case workflow.

Revision ID: 0004_order_case
Revises: 0003_bootstrap_idempotency
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_order_case"
down_revision: str | Sequence[str] | None = "0003_bootstrap_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "case_note_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("external_order_id", sa.String(length=128), nullable=False),
        sa.Column("order_number", sa.String(length=64), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_status", sa.String(length=32), nullable=False),
        sa.Column("fulfillment_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor >= 0", name="ck_orders_amount_minor_nonnegative"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_orders_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id",
            "external_order_id",
            name="uq_orders_organization_external_order",
        ),
    )
    op.create_index("ix_orders_organization_id", "orders", ["organization_id"], unique=False)

    op.create_table(
        "exception_cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("rule_key", sa.String(length=48), nullable=False),
        sa.Column("case_type", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("assignee_membership_id", sa.String(length=36), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_reason", sa.String(length=64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rule_key IN ('payment_failed', 'refund_review', 'fulfillment_delayed')",
            name="ck_exception_cases_rule_key",
        ),
        sa.CheckConstraint(
            "case_type IN ('payment', 'refund', 'fulfillment')",
            name="ck_exception_cases_case_type",
        ),
        sa.CheckConstraint(
            "severity IN ('high', 'medium')",
            name="ck_exception_cases_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'assigned', 'resolved')",
            name="ck_exception_cases_status",
        ),
        sa.CheckConstraint(
            "(rule_key = 'payment_failed' AND case_type = 'payment' AND severity = 'high') "
            "OR (rule_key = 'refund_review' AND case_type = 'refund' AND severity = 'medium') "
            "OR (rule_key = 'fulfillment_delayed' AND case_type = 'fulfillment' "
            "AND severity = 'high')",
            name="ck_exception_cases_rule_shape",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND assignee_membership_id IS NULL "
            "AND resolution_reason IS NULL AND resolved_at IS NULL) "
            "OR (status = 'assigned' AND assignee_membership_id IS NOT NULL "
            "AND resolution_reason IS NULL AND resolved_at IS NULL) "
            "OR (status = 'resolved' AND resolution_reason IS NOT NULL "
            "AND resolved_at IS NOT NULL)",
            name="ck_exception_cases_lifecycle_fields",
        ),
        sa.CheckConstraint(
            "resolution_reason IS NULL "
            "OR (rule_key = 'payment_failed' AND resolution_reason IN "
            "('payment_recovered', 'customer_contacted', 'order_cancelled')) "
            "OR (rule_key = 'refund_review' AND resolution_reason IN "
            "('refund_approved', 'refund_rejected', 'more_information_requested')) "
            "OR (rule_key = 'fulfillment_delayed' AND resolution_reason IN "
            "('carrier_updated', 'replacement_arranged', 'customer_contacted'))",
            name="ck_exception_cases_resolution_reason",
        ),
        sa.CheckConstraint("version >= 1", name="ck_exception_cases_positive_version"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_exception_cases_organization_order",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "assignee_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_exception_cases_organization_assignee",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_exception_cases_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id",
            "source_event_id",
            "rule_key",
            name="uq_exception_cases_organization_event_rule",
        ),
    )
    op.create_index(
        "ix_exception_cases_organization_id",
        "exception_cases",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_exception_cases_organization_queue",
        "exception_cases",
        ["organization_id", "status", "due_at"],
        unique=False,
    )
    op.create_index(
        "ix_exception_cases_organization_assignee",
        "exception_cases",
        ["organization_id", "assignee_membership_id", "status"],
        unique=False,
    )

    op.create_table(
        "case_notes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("author_membership_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id", "case_id"],
            ["exception_cases.organization_id", "exception_cases.id"],
            name="fk_case_notes_organization_case",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "author_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_case_notes_organization_author",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_case_notes_organization_id_id"),
    )
    op.create_index(
        "ix_case_notes_organization_id", "case_notes", ["organization_id"], unique=False
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("actor_membership_id", sa.String(length=36), nullable=True),
        sa.Column("action_key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("object_id", sa.String(length=36), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=True),
        sa.Column("changes_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "object_version IS NULL OR object_version >= 1",
            name="ck_audit_events_positive_object_version",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_audit_events_organization_actor",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "action_key",
            name="uq_audit_events_organization_action_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "object_type",
            "object_id",
            "object_version",
            name="uq_audit_events_organization_object_version",
        ),
    )
    op.create_index(
        "ix_audit_events_organization_id", "audit_events", ["organization_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_organization_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_case_notes_organization_id", table_name="case_notes")
    op.drop_table("case_notes")
    op.drop_index("ix_exception_cases_organization_assignee", table_name="exception_cases")
    op.drop_index("ix_exception_cases_organization_queue", table_name="exception_cases")
    op.drop_index("ix_exception_cases_organization_id", table_name="exception_cases")
    op.drop_table("exception_cases")
    op.drop_index("ix_orders_organization_id", table_name="orders")
    op.drop_table("orders")
    op.drop_column("organizations", "case_note_count")
