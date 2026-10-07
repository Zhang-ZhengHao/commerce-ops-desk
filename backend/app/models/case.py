"""Exception cases implement the small, explicit operations workflow."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExceptionCase(Base):
    __tablename__ = "exception_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "order_id"],
            ["orders.organization_id", "orders.id"],
            name="fk_exception_cases_organization_order",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assignee_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_exception_cases_organization_assignee",
        ),
        CheckConstraint(
            "rule_key IN ('payment_failed', 'refund_review', 'fulfillment_delayed')",
            name="ck_exception_cases_rule_key",
        ),
        CheckConstraint(
            "case_type IN ('payment', 'refund', 'fulfillment')",
            name="ck_exception_cases_case_type",
        ),
        CheckConstraint(
            "severity IN ('high', 'medium')",
            name="ck_exception_cases_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'assigned', 'resolved')",
            name="ck_exception_cases_status",
        ),
        CheckConstraint(
            "(rule_key = 'payment_failed' AND case_type = 'payment' AND severity = 'high') "
            "OR (rule_key = 'refund_review' AND case_type = 'refund' AND severity = 'medium') "
            "OR (rule_key = 'fulfillment_delayed' AND case_type = 'fulfillment' "
            "AND severity = 'high')",
            name="ck_exception_cases_rule_shape",
        ),
        CheckConstraint(
            "(status = 'open' AND assignee_membership_id IS NULL "
            "AND resolution_reason IS NULL AND resolved_at IS NULL) "
            "OR (status = 'assigned' AND assignee_membership_id IS NOT NULL "
            "AND resolution_reason IS NULL AND resolved_at IS NULL) "
            "OR (status = 'resolved' AND resolution_reason IS NOT NULL "
            "AND resolved_at IS NOT NULL)",
            name="ck_exception_cases_lifecycle_fields",
        ),
        CheckConstraint(
            "resolution_reason IS NULL "
            "OR (rule_key = 'payment_failed' AND resolution_reason IN "
            "('payment_recovered', 'customer_contacted', 'order_cancelled')) "
            "OR (rule_key = 'refund_review' AND resolution_reason IN "
            "('refund_approved', 'refund_rejected', 'more_information_requested')) "
            "OR (rule_key = 'fulfillment_delayed' AND resolution_reason IN "
            "('carrier_updated', 'replacement_arranged', 'customer_contacted'))",
            name="ck_exception_cases_resolution_reason",
        ),
        CheckConstraint("version >= 1", name="ck_exception_cases_positive_version"),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_exception_cases_organization_id_id",
        ),
        UniqueConstraint(
            "organization_id",
            "source_event_id",
            "rule_key",
            name="uq_exception_cases_organization_event_rule",
        ),
        Index(
            "ix_exception_cases_organization_queue",
            "organization_id",
            "status",
            "due_at",
        ),
        Index(
            "ix_exception_cases_organization_assignee",
            "organization_id",
            "assignee_membership_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(48), nullable=False)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    assignee_membership_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
