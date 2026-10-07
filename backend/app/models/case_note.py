"""Investigation notes are append-only workflow records."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CaseNote(Base):
    __tablename__ = "case_notes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "case_id"],
            ["exception_cases.organization_id", "exception_cases.id"],
            name="fk_case_notes_organization_case",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "author_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name="fk_case_notes_organization_author",
        ),
        UniqueConstraint("organization_id", "id", name="uq_case_notes_organization_id_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[str] = mapped_column(String(36), nullable=False)
    author_membership_id: Mapped[str] = mapped_column(String(36), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
