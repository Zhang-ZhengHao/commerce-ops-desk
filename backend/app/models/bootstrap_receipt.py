"""Retry records for source-scoped public workspace bootstrap commands."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BootstrapReceipt(Base):
    __tablename__ = "bootstrap_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "result_session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_bootstrap_receipts_organization_result_session",
            ondelete="CASCADE",
        ),
    )

    source_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
