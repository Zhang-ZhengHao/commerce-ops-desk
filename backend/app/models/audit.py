"""Application-created audit events are append-only and retry-stable."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
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


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_audit_events_organization_actor",
        ),
        UniqueConstraint(
            "organization_id",
            "action_key",
            name="uq_audit_events_organization_action_key",
        ),
        UniqueConstraint(
            "organization_id",
            "object_type",
            "object_id",
            "object_version",
            name="uq_audit_events_organization_object_version",
        ),
        CheckConstraint(
            "object_version IS NULL OR object_version >= 1",
            name="ck_audit_events_positive_object_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_membership_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    object_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
