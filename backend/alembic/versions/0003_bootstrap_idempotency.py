"""Add source-scoped workspace bootstrap receipts.

Revision ID: 0003_bootstrap_idempotency
Revises: 0002_demo_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_bootstrap_idempotency"
down_revision: str | Sequence[str] | None = "0002_demo_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bootstrap_receipts",
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("result_session_id", sa.String(length=36), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "result_session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_bootstrap_receipts_organization_result_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_digest", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("bootstrap_receipts")
