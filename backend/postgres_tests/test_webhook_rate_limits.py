"""PostgreSQL proofs for the pre-authentication webhook source limiter."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import RateLimit
from app.repositories.rate_limits import claim_fixed_window_slot
from app.services.webhook_ingress import (
    WebhookIngressRateLimitExceeded,
    enforce_webhook_source_limit,
)

if TYPE_CHECKING:
    from postgres_tests.conftest import PostgresAppHarness

WEBHOOK_MASTER_SECRET = "0123456789abcdef0123456789abcdef"
NORMALIZED_SOURCE = "198.51.100.23"
SOURCE_DIGEST = "f5e9a8351835bd8eaeb885a36b7474aeb3ea28483fedecfddd1db5f099592c37"
NOW = datetime(2026, 10, 7, 12, 34, 20, tzinfo=UTC)


def test_postgresql_repository_defers_commit_and_service_persists_only_keyed_source(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
        webhook_source_minute_limit=2,
    )
    window_start = NOW.replace(second=0, microsecond=0)
    rolled_back_digest = "a" * 64

    with Session(harness.engine) as database:
        assert claim_fixed_window_slot(
            database,
            source_digest=rolled_back_digest,
            window_start=window_start,
            now=NOW,
            maximum=1,
        )
        database.rollback()

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source=NORMALIZED_SOURCE,
        now=NOW,
    )

    with Session(harness.engine) as database:
        rows = database.scalars(select(RateLimit)).all()

    assert [(row.source_digest, row.count) for row in rows] == [(SOURCE_DIGEST, 1)]
    stored_values = " ".join(
        f"{row.source_digest} {row.window_start} {row.count} {row.updated_at}" for row in rows
    )
    assert NORMALIZED_SOURCE not in stored_values
    assert WEBHOOK_MASTER_SECRET not in stored_values
    assert rolled_back_digest not in stored_values


def test_concurrent_postgresql_attempts_cannot_overshoot_the_webhook_source_limit(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
        webhook_source_minute_limit=2,
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    barrier = Barrier(4)

    def attempt() -> str:
        barrier.wait(timeout=10)
        try:
            enforce_webhook_source_limit(
                factory,
                settings=harness.settings,
                normalized_source=NORMALIZED_SOURCE,
                now=NOW,
            )
        except WebhookIngressRateLimitExceeded:
            return "limited"
        return "accepted"

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(attempt) for _ in range(4)]
        outcomes = sorted(future.result(timeout=15) for future in futures)

    assert outcomes == ["accepted", "accepted", "limited", "limited"]
    with Session(harness.engine) as database:
        count = database.scalar(
            select(func.sum(RateLimit.count)).where(RateLimit.source_digest == SOURCE_DIGEST)
        )
    assert count == 2
