"""Persistent demo-workspace abuse and capacity limits."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast
from uuid import uuid4

from conftest import SAME_ORIGIN, AppHarness, FrozenClock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.services.demo_workspaces import DatabaseExpiredWorkspaceCleaner


def _bootstrap(
    client: TestClient,
    *,
    forwarded_for: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    headers = {
        "Origin": SAME_ORIGIN,
        "Idempotency-Key": idempotency_key or f"rate-limit-{uuid4()}",
    }
    if forwarded_for is not None:
        headers["X-Forwarded-For"] = forwarded_for
    return client.post(
        "/api/demo/workspaces",
        headers=headers,
        json={"initial_role": "manager"},
    )


def test_demo_limit_defaults_match_the_public_safety_boundary(app_harness: AppHarness) -> None:
    assert app_harness.settings.demo_source_hourly_limit == 10
    assert app_harness.settings.demo_active_workspace_limit == 500
    assert app_harness.settings.demo_workspace_ttl_hours == 4


def test_source_hourly_limit_is_persistent_across_app_restarts(
    app_harness_factory: Callable[..., AppHarness],
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "persistent-rate-limit.sqlite3"
    clock = FrozenClock()
    first_process = app_harness_factory(database_path=database_path, clock=clock)

    with first_process.client(source_ip="198.51.100.7") as client:
        accepted = [_bootstrap(client) for _ in range(10)]
    assert [response.status_code for response in accepted] == [201] * 10

    restarted_process = app_harness_factory(
        database_path=database_path,
        clock=clock,
        migrate=False,
    )
    with restarted_process.client(source_ip="198.51.100.7") as client:
        limited = _bootstrap(client)

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


def test_source_limit_opens_a_new_window_without_sleeping(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    clock = FrozenClock()
    harness = app_harness_factory(clock=clock)

    with harness.client(source_ip="198.51.100.8") as client:
        for _ in range(10):
            assert _bootstrap(client).status_code == 201
        assert _bootstrap(client).status_code == 429

        clock.advance(hours=1, seconds=1)
        next_window = _bootstrap(client)

    assert next_window.status_code == 201


def test_untrusted_forwarded_addresses_cannot_bypass_the_source_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(trusted_proxy_cidrs=())

    with harness.client(source_ip="198.51.100.9") as client:
        accepted = [
            _bootstrap(client, forwarded_for=f"203.0.113.{index}") for index in range(1, 11)
        ]
        limited = _bootstrap(client, forwarded_for="203.0.113.250")

    assert [response.status_code for response in accepted] == [201] * 10
    assert limited.status_code == 429


def test_trusted_direct_proxy_uses_forwarded_source_without_storing_the_address(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        demo_source_hourly_limit=1,
        trusted_proxy_cidrs=("192.0.2.0/24",),
    )

    with harness.client(source_ip="192.0.2.3") as client:
        first_source = _bootstrap(client, forwarded_for="198.51.100.21")
        second_source = _bootstrap(client, forwarded_for="198.51.100.22")
        repeated_source = _bootstrap(client, forwarded_for="198.51.100.21")

    assert first_source.status_code == 201
    assert second_source.status_code == 201
    assert repeated_source.status_code == 429

    with sqlite3.connect(harness.database_path) as connection:
        stored_digests = connection.execute("SELECT source_digest FROM rate_limits").fetchall()

    assert stored_digests
    assert all(len(digest) == 64 for (digest,) in stored_digests)
    assert all("198.51.100" not in digest for (digest,) in stored_digests)


def test_trusted_proxy_chain_ignores_spoofed_leftmost_addresses(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        demo_source_hourly_limit=2,
        trusted_proxy_cidrs=("192.0.2.0/24",),
    )

    with harness.client(source_ip="192.0.2.3") as client:
        responses = [
            _bootstrap(
                client,
                forwarded_for=f"203.0.113.{index}, 198.51.100.77, 192.0.2.4",
            )
            for index in range(1, 4)
        ]

    assert [response.status_code for response in responses] == [201, 201, 429]


def test_invalid_trusted_proxy_chain_falls_back_to_the_direct_source(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        demo_source_hourly_limit=1,
        trusted_proxy_cidrs=("192.0.2.0/24",),
    )

    with harness.client(source_ip="192.0.2.3") as client:
        first = _bootstrap(client, forwarded_for="203.0.113.1, not-an-ip")
        repeated = _bootstrap(client, forwarded_for="203.0.113.2, still-not-an-ip")

    assert first.status_code == 201
    assert repeated.status_code == 429


def test_concurrent_bootstrap_requests_cannot_overshoot_the_source_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_source_hourly_limit=1)
    barrier = Barrier(2)

    def bootstrap_concurrently() -> int:
        with harness.client(source_ip="198.51.100.31") as client:
            barrier.wait(timeout=10)
            return int(_bootstrap(client).status_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _: bootstrap_concurrently(), range(2)))

    assert statuses == [201, 429]
    with sqlite3.connect(harness.database_path) as connection:
        organization_count = connection.execute("SELECT COUNT(*) FROM organizations").fetchone()
    assert organization_count == (1,)


def test_active_workspace_capacity_returns_service_unavailable(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_active_workspace_limit=2)

    with harness.client(source_ip="198.51.100.10") as client:
        first = _bootstrap(client)
        second = _bootstrap(client)
        at_capacity = _bootstrap(client)

    assert first.status_code == 201
    assert second.status_code == 201
    assert at_capacity.status_code == 503


def test_capacity_lock_identity_does_not_depend_on_the_session_secret(
    app_harness_factory: Callable[..., AppHarness],
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stable-capacity-lock.sqlite3"
    first_process = app_harness_factory(
        database_path=database_path,
        session_secret="first-process-secret-that-is-at-least-32-bytes",
    )
    with first_process.client(source_ip="198.51.100.41") as client:
        assert _bootstrap(client).status_code == 201

    second_process = app_harness_factory(
        database_path=database_path,
        migrate=False,
        session_secret="second-process-secret-that-is-at-least-32-bytes",
    )
    with second_process.client(source_ip="198.51.100.42") as client:
        assert _bootstrap(client).status_code == 201

    with sqlite3.connect(database_path) as connection:
        capacity_lock_rows = connection.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE count = 0"
        ).fetchone()

    assert capacity_lock_rows == (1,)


def test_expired_workspace_cleanup_is_an_explicit_unscheduled_service(
    app_harness: AppHarness,
) -> None:
    with app_harness.client() as client:
        bootstrap = _bootstrap(client)
        assert bootstrap.status_code == 201
        switched = client.post(
            "/api/demo/role",
            headers={
                "Origin": SAME_ORIGIN,
                "X-CSRF-Token": bootstrap.json()["csrf_token"],
                "Idempotency-Key": "cleanup-cascade-role-switch",
            },
            json={"role": "agent"},
        )
        assert switched.status_code == 200

    app_harness.clock.advance(hours=4, seconds=1)
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    with factory() as database:
        deleted = DatabaseExpiredWorkspaceCleaner().cleanup(
            database,
            now=app_harness.clock(),
        )
        database.commit()

    with factory() as database:
        remaining_organizations = (
            database.connection().exec_driver_sql("SELECT COUNT(*) FROM organizations").scalar_one()
        )
        remaining_sessions = (
            database.connection().exec_driver_sql("SELECT COUNT(*) FROM sessions").scalar_one()
        )
        remaining_receipts = (
            database.connection()
            .exec_driver_sql("SELECT COUNT(*) FROM command_receipts")
            .scalar_one()
        )
        remaining_integrations = (
            database.connection()
            .exec_driver_sql("SELECT COUNT(*) FROM webhook_integrations")
            .scalar_one()
        )

    assert deleted == 1
    assert remaining_organizations == 0
    assert remaining_sessions == 0
    assert remaining_receipts == 0
    assert remaining_integrations == 0
