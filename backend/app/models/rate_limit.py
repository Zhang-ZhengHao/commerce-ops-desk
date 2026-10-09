"""Persistent, privacy-preserving fixed-window counters."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RateLimit(Base):
    __tablename__ = "rate_limits"
    __table_args__ = (
        Index(
            "ix_rate_limits_window_source",
            "window_start",
            "source_digest",
        ),
    )

    source_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
