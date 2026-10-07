"""Idempotent command records contain safe results, never browser secrets."""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_command_receipts_organization_membership",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "result_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_command_receipts_organization_result_membership",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "result_session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_command_receipts_organization_result_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "membership_id",
            "command_type",
            "idempotency_key",
            name="uq_command_receipts_membership_command_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    membership_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command_type: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_membership_id: Mapped[str] = mapped_column(String(36), nullable=False)
    result_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
