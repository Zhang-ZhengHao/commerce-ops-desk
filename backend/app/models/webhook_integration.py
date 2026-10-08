"""Server-owned synthetic webhook integration metadata."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WebhookIntegration(Base):
    __tablename__ = "webhook_integrations"
    __table_args__ = (
        CheckConstraint(
            "provider = 'synthetic'",
            name="ck_webhook_integrations_provider",
        ),
        CheckConstraint(
            "key_version >= 1",
            name="ck_webhook_integrations_positive_key_version",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_webhook_integrations_organization_id_id",
        ),
        UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_webhook_integrations_organization_provider",
        ),
        Index("ix_webhook_integrations_id_enabled", "id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "organizations.id",
            name="fk_webhook_integrations_organization",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
