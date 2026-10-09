"""Bounded maintenance operations for hosted demo data."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import delete, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.models import RateLimit
from app.services.demo_workspaces import (
    GLOBAL_CAPACITY_LOCK_DIGEST,
    DatabaseExpiredWorkspaceCleaner,
)

WORKSPACE_BATCH_SIZE = 100
WORKSPACE_CYCLE_LIMIT = 500
RATE_LIMIT_BATCH_SIZE = 500
RATE_LIMIT_CYCLE_LIMIT = 2_000
MAINTENANCE_INTERVAL_SECONDS = 60 * 60
LOGGER = logging.getLogger(__name__)

MaintenanceClock = Callable[[], datetime]
MaintenanceWait = Callable[[asyncio.Event, float], Awaitable[bool]]


class MaintenanceCycle(Protocol):
    def __call__(
        self,
        session_factory: sessionmaker[Session],
        /,
        *,
        now: datetime,
    ) -> MaintenanceResult | None: ...


@dataclass(frozen=True)
class MaintenanceResult:
    expired_workspaces_deleted: int
    obsolete_rate_limits_deleted: int
    failed_phase: str | None = None


def delete_expired_workspace_batch(db: Session, *, now: datetime) -> int:
    """Delete one ordered, bounded batch without owning its transaction."""
    return DatabaseExpiredWorkspaceCleaner().cleanup(db, now=now)


def delete_obsolete_rate_limit_batch(db: Session, *, now: datetime) -> int:
    """Delete rate-limit windows strictly older than the retention boundary."""
    cutoff = now - timedelta(hours=24)
    candidate = aliased(RateLimit)
    obsolete_keys = (
        select(candidate.window_start, candidate.source_digest)
        .where(
            candidate.window_start < cutoff,
            candidate.source_digest != GLOBAL_CAPACITY_LOCK_DIGEST,
        )
        .order_by(candidate.window_start, candidate.source_digest)
        .limit(RATE_LIMIT_BATCH_SIZE)
    )
    result = db.execute(
        delete(RateLimit)
        .where(
            RateLimit.window_start < cutoff,
            RateLimit.source_digest != GLOBAL_CAPACITY_LOCK_DIGEST,
            tuple_(RateLimit.window_start, RateLimit.source_digest).in_(obsolete_keys),
        )
        .execution_options(synchronize_session=False)
    )
    cursor_result = cast(CursorResult[Any], result)
    return int(cursor_result.rowcount or 0)


def _log_batch_failure(
    *,
    phase: str,
    batch_number: int,
    row_count: int,
    error: Exception,
) -> None:
    LOGGER.error(
        "maintenance_batch_failed",
        extra={
            "phase": phase,
            "batch_number": batch_number,
            "row_count": row_count,
            "exception_type": type(error).__name__,
        },
    )


def run_maintenance_cycle(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
) -> MaintenanceResult:
    """Run a bounded workspace-maintenance cycle in short transactions."""
    deleted_total = 0
    maximum_batches = WORKSPACE_CYCLE_LIMIT // WORKSPACE_BATCH_SIZE

    for _batch_number in range(maximum_batches):
        with session_factory() as database:
            try:
                deleted = delete_expired_workspace_batch(database, now=now)
                database.commit()
            except Exception as error:
                database.rollback()
                _log_batch_failure(
                    phase="expired_workspaces",
                    batch_number=_batch_number + 1,
                    row_count=deleted_total,
                    error=error,
                )
                return MaintenanceResult(
                    expired_workspaces_deleted=deleted_total,
                    obsolete_rate_limits_deleted=0,
                    failed_phase="expired_workspaces",
                )
        deleted_total += deleted
        if deleted < WORKSPACE_BATCH_SIZE:
            break

    obsolete_rate_limits_deleted = 0
    maximum_rate_limit_batches = RATE_LIMIT_CYCLE_LIMIT // RATE_LIMIT_BATCH_SIZE
    for _batch_number in range(maximum_rate_limit_batches):
        with session_factory() as database:
            try:
                deleted = delete_obsolete_rate_limit_batch(database, now=now)
                database.commit()
            except Exception as error:
                database.rollback()
                _log_batch_failure(
                    phase="obsolete_rate_limits",
                    batch_number=_batch_number + 1,
                    row_count=obsolete_rate_limits_deleted,
                    error=error,
                )
                return MaintenanceResult(
                    expired_workspaces_deleted=deleted_total,
                    obsolete_rate_limits_deleted=obsolete_rate_limits_deleted,
                    failed_phase="obsolete_rate_limits",
                )
        obsolete_rate_limits_deleted += deleted
        if deleted < RATE_LIMIT_BATCH_SIZE:
            break

    return MaintenanceResult(
        expired_workspaces_deleted=deleted_total,
        obsolete_rate_limits_deleted=obsolete_rate_limits_deleted,
    )


async def wait_for_maintenance_stop(
    stop_event: asyncio.Event,
    delay_seconds: float,
) -> bool:
    """Wait for shutdown and report whether it arrived before the interval."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return False
    return True


async def run_maintenance_scheduler(
    *,
    session_factory: sessionmaker[Session],
    clock: MaintenanceClock,
    cycle: MaintenanceCycle = run_maintenance_cycle,
    stop_event: asyncio.Event,
    wait: MaintenanceWait = wait_for_maintenance_stop,
    interval_seconds: float = MAINTENANCE_INTERVAL_SECONDS,
) -> None:
    """Run maintenance immediately and after each interval until shutdown."""
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(cycle, session_factory, now=clock())
        except Exception as error:
            LOGGER.warning(
                "maintenance_cycle_failed",
                extra={"exception_type": type(error).__name__},
            )
        if await wait(stop_event, interval_seconds):
            break
