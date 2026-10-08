"""Durable, privacy-preserving source limits for webhook ingress."""

import hmac
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest
from conftest import AppHarness
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Scope

from app.models import RateLimit
from app.repositories.rate_limits import claim_fixed_window_slot
from app.services.demo_workspaces import client_source_address
from app.services.webhook_ingress import (
    WebhookIngressRateLimitExceeded,
    derive_webhook_source_digest,
    enforce_webhook_source_limit,
)

WEBHOOK_MASTER_SECRET = b"0123456789abcdef0123456789abcdef"
NORMALIZED_SOURCE = "198.51.100.23"
SOURCE_DIGEST = "f5e9a8351835bd8eaeb885a36b7474aeb3ea28483fedecfddd1db5f099592c37"
NOW = datetime(2026, 10, 7, 12, 34, 20, tzinfo=UTC)


def _request(*, source_ip: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/webhooks/synthetic/test",
        "raw_path": b"/api/webhooks/synthetic/test",
        "query_string": b"",
        "headers": headers,
        "client": (source_ip, 50_000),
        "server": ("commerceops.test", 443),
    }
    return Request(scope)


def test_webhook_source_digest_matches_an_independent_fixed_vector() -> None:
    assert (
        derive_webhook_source_digest(
            WEBHOOK_MASTER_SECRET,
            normalized_source=NORMALIZED_SOURCE,
        )
        == SOURCE_DIGEST
    )


def test_webhook_source_digest_is_domain_separated_lowercase_sha256() -> None:
    digest = derive_webhook_source_digest(
        WEBHOOK_MASTER_SECRET,
        normalized_source=NORMALIZED_SOURCE,
    )
    raw_source_digest = hmac.digest(
        WEBHOOK_MASTER_SECRET,
        NORMALIZED_SOURCE.encode("ascii"),
        "sha256",
    ).hex()
    browser_source_digest = hmac.digest(
        WEBHOOK_MASTER_SECRET,
        f"commerce-ops:rate-limit-source:{NORMALIZED_SOURCE}".encode("ascii"),
        "sha256",
    ).hex()

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest not in {raw_source_digest, browser_source_digest}
    assert NORMALIZED_SOURCE not in digest


def test_webhook_source_digest_changes_with_source_or_master_secret() -> None:
    baseline = derive_webhook_source_digest(
        WEBHOOK_MASTER_SECRET,
        normalized_source=NORMALIZED_SOURCE,
    )

    assert baseline != derive_webhook_source_digest(
        WEBHOOK_MASTER_SECRET,
        normalized_source="198.51.100.24",
    )
    assert baseline != derive_webhook_source_digest(
        b"fedcba9876543210fedcba9876543210",
        normalized_source=NORMALIZED_SOURCE,
    )


def test_webhook_source_digest_rejects_a_short_master_secret_without_reflection() -> None:
    secret = b"SHORT-WEBHOOK-SECRET-CANARY"

    with pytest.raises(ValueError) as raised:
        derive_webhook_source_digest(secret, normalized_source=NORMALIZED_SOURCE)

    assert str(raised.value) == "webhook master secret must contain at least 32 bytes"
    assert secret.decode("ascii") not in repr(raised.value)
    assert raised.value.__context__ is None


def test_webhook_source_digest_rejects_non_utf8_source_without_retaining_it() -> None:
    source = "source-SECRET-CANARY-\ud800"

    with pytest.raises(ValueError) as raised:
        derive_webhook_source_digest(
            WEBHOOK_MASTER_SECRET,
            normalized_source=source,
        )

    assert str(raised.value) == "normalized webhook source must be valid UTF-8"
    assert source not in repr(raised.value)
    assert raised.value.__context__ is None


def test_fixed_window_repository_leaves_transaction_ownership_to_its_caller(
    app_harness: AppHarness,
) -> None:
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        assert claim_fixed_window_slot(
            database,
            source_digest=SOURCE_DIGEST,
            window_start=NOW.replace(second=0, microsecond=0),
            now=NOW,
            maximum=1,
        )
        persisted_inside_transaction = database.scalar(select(func.count()).select_from(RateLimit))
        assert persisted_inside_transaction == 1
        database.rollback()

    with factory() as database:
        persisted_after_rollback = database.scalar(select(func.count()).select_from(RateLimit))

    assert persisted_after_rollback == 0


def test_fixed_window_repository_never_increments_past_the_maximum(
    app_harness: AppHarness,
) -> None:
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    window_start = NOW.replace(second=0, microsecond=0)

    with factory.begin() as database:
        assert claim_fixed_window_slot(
            database,
            source_digest=SOURCE_DIGEST,
            window_start=window_start,
            now=NOW,
            maximum=2,
        )
        assert claim_fixed_window_slot(
            database,
            source_digest=SOURCE_DIGEST,
            window_start=window_start,
            now=NOW + timedelta(seconds=1),
            maximum=2,
        )
        assert not claim_fixed_window_slot(
            database,
            source_digest=SOURCE_DIGEST,
            window_start=window_start,
            now=NOW + timedelta(seconds=2),
            maximum=2,
        )

    with factory() as database:
        row = database.get(RateLimit, (SOURCE_DIGEST, window_start))

    assert row is not None
    assert row.count == 2
    assert row.updated_at == (NOW + timedelta(seconds=1)).replace(tzinfo=None)


def test_webhook_limit_commits_before_a_later_business_rollback(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=2,
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    window_start = NOW.replace(second=0, microsecond=0)

    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source=NORMALIZED_SOURCE,
        now=NOW,
    )
    with factory() as later_business_transaction:
        later_business_transaction.add(
            RateLimit(
                source_digest="b" * 64,
                window_start=window_start,
                count=99,
                updated_at=NOW,
            )
        )
        later_business_transaction.flush()
        later_business_transaction.rollback()

    with factory() as database:
        persisted_rows = database.scalars(select(RateLimit)).all()

    assert [(row.source_digest, row.count) for row in persisted_rows] == [(SOURCE_DIGEST, 1)]


def test_webhook_limit_uses_utc_minute_windows_and_exact_retry_after(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=1,
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    minute_start = NOW.replace(second=0, microsecond=0)

    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source="198.51.100.40",
        now=minute_start,
    )
    with pytest.raises(WebhookIngressRateLimitExceeded) as exact_boundary:
        enforce_webhook_source_limit(
            factory,
            settings=harness.settings,
            normalized_source="198.51.100.40",
            now=minute_start,
        )

    near_boundary = minute_start + timedelta(seconds=59, microseconds=100_000)
    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source="198.51.100.41",
        now=near_boundary,
    )
    with pytest.raises(WebhookIngressRateLimitExceeded) as final_second:
        enforce_webhook_source_limit(
            factory,
            settings=harness.settings,
            normalized_source="198.51.100.41",
            now=near_boundary,
        )

    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source="198.51.100.40",
        now=minute_start + timedelta(minutes=1),
    )

    assert exact_boundary.value.retry_after == 60
    assert final_second.value.retry_after == 1
    assert str(exact_boundary.value) == "webhook ingress source limit exceeded"
    with factory() as database:
        rows = database.scalars(select(RateLimit)).all()
    assert sorted(row.count for row in rows) == [1, 1, 1]


def test_webhook_limit_normalizes_an_offset_clock_before_bucketing(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=1,
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    offset_now = datetime(
        2026,
        10,
        7,
        18,
        4,
        20,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )

    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source=NORMALIZED_SOURCE,
        now=offset_now,
    )
    with pytest.raises(WebhookIngressRateLimitExceeded) as limited:
        enforce_webhook_source_limit(
            factory,
            settings=harness.settings,
            normalized_source=NORMALIZED_SOURCE,
            now=offset_now,
        )

    with factory() as database:
        row = database.scalar(select(RateLimit))
    assert row is not None
    assert row.window_start == NOW.replace(second=0, microsecond=0, tzinfo=None)
    assert row.updated_at == NOW.replace(tzinfo=None)
    assert limited.value.retry_after == 40


def test_webhook_limit_identity_survives_restart_and_session_secret_rotation(
    app_harness_factory: Callable[..., AppHarness],
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "webhook-rate-limit-restart.sqlite3"
    first_process = app_harness_factory(
        database_path=database_path,
        session_secret="first-browser-session-secret-at-least-32-bytes",
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=2,
    )
    first_factory = cast(sessionmaker[Session], first_process.app.state.session_factory)
    enforce_webhook_source_limit(
        first_factory,
        settings=first_process.settings,
        normalized_source=NORMALIZED_SOURCE,
        now=NOW,
    )

    second_process = app_harness_factory(
        database_path=database_path,
        migrate=False,
        session_secret="second-browser-session-secret-at-least-32-bytes",
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=2,
    )
    second_factory = cast(sessionmaker[Session], second_process.app.state.session_factory)
    enforce_webhook_source_limit(
        second_factory,
        settings=second_process.settings,
        normalized_source=NORMALIZED_SOURCE,
        now=NOW,
    )

    with second_factory() as database:
        rows = database.scalars(select(RateLimit)).all()

    assert [(row.source_digest, row.count) for row in rows] == [(SOURCE_DIGEST, 2)]
    stored_values = " ".join(
        f"{row.source_digest} {row.window_start} {row.count} {row.updated_at}" for row in rows
    )
    assert NORMALIZED_SOURCE not in stored_values
    assert WEBHOOK_MASTER_SECRET.decode("ascii") not in stored_values


def test_webhook_limit_requires_an_aware_clock_and_configured_secret(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    without_secret = app_harness_factory()
    without_secret_factory = cast(
        sessionmaker[Session],
        without_secret.app.state.session_factory,
    )
    with pytest.raises(RuntimeError, match="master secret is unavailable"):
        enforce_webhook_source_limit(
            without_secret_factory,
            settings=without_secret.settings,
            normalized_source=NORMALIZED_SOURCE,
            now=NOW,
        )

    configured = app_harness_factory(webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"))
    configured_factory = cast(sessionmaker[Session], configured.app.state.session_factory)
    with pytest.raises(ValueError, match="timezone-aware"):
        enforce_webhook_source_limit(
            configured_factory,
            settings=configured.settings,
            normalized_source=NORMALIZED_SOURCE,
            now=NOW.replace(tzinfo=None),
        )

    for factory in (without_secret_factory, configured_factory):
        with factory() as database:
            assert database.scalar(select(func.count()).select_from(RateLimit)) == 0


def test_concurrent_sqlite_attempts_cannot_overshoot_the_webhook_source_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=1,
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    barrier = Barrier(2)

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

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: attempt(), range(2)))

    assert outcomes == ["accepted", "limited"]
    with factory() as database:
        row = database.scalar(select(RateLimit))
    assert row is not None
    assert row.count == 1


def test_trusted_proxy_chain_shares_the_rightmost_untrusted_webhook_bucket(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"),
        webhook_source_minute_limit=1,
        trusted_proxy_cidrs=("192.0.2.0/24",),
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    first_source = client_source_address(
        _request(
            source_ip="192.0.2.3",
            forwarded_for="203.0.113.1, 198.51.100.77, 192.0.2.4",
        ),
        harness.settings,
    )
    second_source = client_source_address(
        _request(
            source_ip="192.0.2.3",
            forwarded_for="203.0.113.2, 198.51.100.77, 192.0.2.4",
        ),
        harness.settings,
    )

    assert first_source == second_source == "198.51.100.77"
    enforce_webhook_source_limit(
        factory,
        settings=harness.settings,
        normalized_source=first_source,
        now=NOW,
    )
    with pytest.raises(WebhookIngressRateLimitExceeded):
        enforce_webhook_source_limit(
            factory,
            settings=harness.settings,
            normalized_source=second_source,
            now=NOW,
        )

    expected_digest = derive_webhook_source_digest(
        WEBHOOK_MASTER_SECRET,
        normalized_source="198.51.100.77",
    )
    with factory() as database:
        row = database.scalar(select(RateLimit))
    assert row is not None
    assert row.source_digest == expected_digest


def test_equivalent_ipv6_sources_share_the_canonical_webhook_digest(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(webhook_master_secret=WEBHOOK_MASTER_SECRET.decode("ascii"))
    expanded = client_source_address(
        _request(source_ip="2001:0db8:0:0:0:0:0:1"),
        harness.settings,
    )
    compressed = client_source_address(
        _request(source_ip="2001:db8::1"),
        harness.settings,
    )

    assert expanded == compressed == "2001:db8::1"
    assert (
        derive_webhook_source_digest(
            WEBHOOK_MASTER_SECRET,
            normalized_source=expanded,
        )
        == "5a427d76ac1ae1e3ae116ab7ebc80d544c17af8201a8308770c8a3b5b9921350"
    )
