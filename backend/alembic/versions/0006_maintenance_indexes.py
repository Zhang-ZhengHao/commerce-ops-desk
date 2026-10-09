"""Add deterministic maintenance scan indexes.

Revision ID: 0006_maintenance_indexes
Revises: 0005_webhook_inbox
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_maintenance_indexes"
down_revision: str | Sequence[str] | None = "0005_webhook_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_organizations_demo_expiry",
        table_name="organizations",
    )
    op.create_index(
        "ix_organizations_demo_expiry",
        "organizations",
        ["is_demo", "expires_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_rate_limits_window_source",
        "rate_limits",
        ["window_start", "source_digest"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rate_limits_window_source",
        table_name="rate_limits",
    )
    op.drop_index(
        "ix_organizations_demo_expiry",
        table_name="organizations",
    )
    op.create_index(
        "ix_organizations_demo_expiry",
        "organizations",
        ["is_demo", "expires_at"],
        unique=False,
    )
