"""Establish the CommerceOps Desk migration lineage.

Revision ID: 0001_foundation
Revises:
"""

from collections.abc import Sequence

revision: str = "0001_foundation"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish the initial revision without pre-claiming domain tables."""


def downgrade() -> None:
    """Remove the initial revision marker without dropping domain data."""
