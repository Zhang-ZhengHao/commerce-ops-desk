"""Atomic fixed-window counter primitives shared by ingress boundaries."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import RateLimit


def claim_fixed_window_slot(
    db: Session,
    *,
    source_digest: str,
    window_start: datetime,
    now: datetime,
    maximum: int,
) -> bool:
    """Atomically increment below a limit without owning the transaction."""

    values = {
        "source_digest": source_digest,
        "window_start": window_start,
        "count": 1,
        "updated_at": now,
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_statement = sqlite_insert(RateLimit).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_update(
            index_elements=[RateLimit.source_digest, RateLimit.window_start],
            set_={"count": RateLimit.count + 1, "updated_at": now},
            where=RateLimit.count < maximum,
        )
        result = db.execute(sqlite_statement)
    elif dialect_name == "postgresql":
        postgresql_statement = postgresql_insert(RateLimit).values(**values)
        postgresql_statement = postgresql_statement.on_conflict_do_update(
            index_elements=[RateLimit.source_digest, RateLimit.window_start],
            set_={"count": RateLimit.count + 1, "updated_at": now},
            where=RateLimit.count < maximum,
        )
        persisted_count = db.scalar(postgresql_statement.returning(RateLimit.count))
        return persisted_count is not None
    else:  # Settings currently prevents reaching an unsupported backend.
        raise RuntimeError("unsupported rate-limit database")

    cursor_result = cast(CursorResult[Any], result)
    return bool(cursor_result.rowcount)
