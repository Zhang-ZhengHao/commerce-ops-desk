"""Bounded webhook inbox metadata without raw request material."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

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


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            "event_type = 'payment.failed'",
            name="ck_webhook_events_event_type",
        ),
        CheckConstraint(
            PAYLOAD_DIGEST_CHECK,
            name="ck_webhook_events_payload_digest",
        ),
        CheckConstraint(
            "(order_id IS NULL AND case_id IS NULL AND processed_at IS NULL) "
            "OR (order_id IS NOT NULL AND case_id IS NOT NULL "
            "AND processed_at IS NOT NULL)",
            name="ck_webhook_events_processing_state",
        ),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["webhook_integrations.organization_id", "webhook_integrations.id"],
            name="fk_webhook_events_organization_integration",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_webhook_events_organization_order",
        ),
        ForeignKeyConstraint(
            ["organization_id", "case_id"],
            ["exception_cases.organization_id", "exception_cases.id"],
            name="fk_webhook_events_organization_case",
        ),
        UniqueConstraint(
            "organization_id",
            "integration_id",
            "external_event_id",
            name="uq_webhook_events_organization_integration_external_event",
        ),
        Index(
            "ix_webhook_events_organization_received_at",
            "organization_id",
            "received_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "organizations.id",
            name="fk_webhook_events_organization",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    integration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(68), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
