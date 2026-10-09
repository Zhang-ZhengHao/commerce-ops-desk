"""Bounded maintenance contracts for hosted demo data."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from conftest import AppHarness
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import Organization, RateLimit
from app.services import maintenance
from app.services.demo_workspaces import (
    GLOBAL_CAPACITY_LOCK_DIGEST,
    DatabaseExpiredWorkspaceCleaner,
)
from app.services.maintenance import (
    MAINTENANCE_INTERVAL_SECONDS,
    run_maintenance_cycle,
    run_maintenance_scheduler,
)


def test_expired_workspace_cleanup_deletes_at_most_one_hundred_organizations(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    expired_ids = [f"expired-demo-{index:03d}" for index in range(101)]

    with factory() as database:
        database.add_all(
            [
                Organization(
                    id=organization_id,
                    name=f"Expired demo {index:03d}",
                    is_demo=True,
                    created_at=now - timedelta(days=1),
                    expires_at=now - timedelta(seconds=101 - index),
                )
                for index, organization_id in enumerate(expired_ids)
            ]
            + [
                Organization(
                    id="active-demo",
                    name="Active demo",
                    is_demo=True,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                ),
                Organization(
                    id="expired-customer",
                    name="Expired non-demo customer",
                    is_demo=False,
                    created_at=now - timedelta(days=1),
                    expires_at=now - timedelta(hours=1),
                ),
            ]
        )
        database.commit()

    with factory() as database:
        deleted = DatabaseExpiredWorkspaceCleaner().cleanup(database, now=now)
        database.commit()

    with factory() as database:
        remaining_ids = set(database.scalars(select(Organization.id)))

    assert deleted == 100
    assert remaining_ids == {
        expired_ids[-1],
        "active-demo",
        "expired-customer",
    }


def test_expired_workspace_batch_is_one_guarded_delete_statement(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    engine = app_harness.app.state.database_engine

    with factory() as database:
        database.add(
            Organization(
                id="single-statement-expired-demo",
                name="Single statement expired demo",
                is_demo=True,
                created_at=now - timedelta(days=1),
                expires_at=now - timedelta(seconds=1),
            )
        )
        database.commit()

    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "DELETE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with factory() as database:
            deleted = DatabaseExpiredWorkspaceCleaner().cleanup(database, now=now)
            database.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert deleted == 1
    assert [statement.lstrip().split(maxsplit=1)[0].upper() for statement in statements] == [
        "DELETE"
    ]
    delete_sql = statements[0].lower()
    assert "organizations.is_demo" in delete_sql
    assert "organizations_1.is_demo" in delete_sql
    assert "organizations.expires_at" in delete_sql
    assert "organizations_1.expires_at" in delete_sql


def test_maintenance_cycle_deletes_at_most_five_hundred_expired_workspaces(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        database.add_all(
            [
                Organization(
                    id=f"cycle-expired-{index:03d}",
                    name=f"Cycle expired demo {index:03d}",
                    is_demo=True,
                    created_at=now - timedelta(days=1),
                    expires_at=now - timedelta(seconds=501 - index),
                )
                for index in range(501)
            ]
        )
        database.commit()

    result = run_maintenance_cycle(factory, now=now)

    with factory() as database:
        remaining_ids = tuple(
            database.scalars(
                select(Organization.id).order_by(
                    Organization.expires_at,
                    Organization.id,
                )
            )
        )

    assert result.expired_workspaces_deleted == 500
    assert remaining_ids == ("cycle-expired-500",)


def test_maintenance_deletes_rate_limits_strictly_older_than_twenty_four_hours(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    cutoff = now - timedelta(hours=24)

    with factory() as database:
        database.add_all(
            [
                RateLimit(
                    source_digest="a" * 64,
                    window_start=cutoff - timedelta(microseconds=1),
                    count=1,
                    updated_at=now,
                ),
                RateLimit(
                    source_digest="b" * 64,
                    window_start=cutoff,
                    count=1,
                    updated_at=now,
                ),
                RateLimit(
                    source_digest="c" * 64,
                    window_start=cutoff + timedelta(microseconds=1),
                    count=1,
                    updated_at=now,
                ),
            ]
        )
        database.commit()

    result = run_maintenance_cycle(factory, now=now)

    with factory() as database:
        remaining_digests = set(database.scalars(select(RateLimit.source_digest)))

    assert result.obsolete_rate_limits_deleted == 1
    assert remaining_digests == {"b" * 64, "c" * 64}


def test_rate_limit_batch_is_one_guarded_delete_statement(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    engine = app_harness.app.state.database_engine

    with factory() as database:
        database.add(
            RateLimit(
                source_digest="e" * 64,
                window_start=now - timedelta(hours=25),
                count=1,
                updated_at=now,
            )
        )
        database.commit()

    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "DELETE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with factory() as database:
            deleted = maintenance.delete_obsolete_rate_limit_batch(database, now=now)
            database.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert deleted == 1
    assert [statement.lstrip().split(maxsplit=1)[0].upper() for statement in statements] == [
        "DELETE"
    ]
    delete_sql = statements[0].lower()
    assert "rate_limits.window_start" in delete_sql
    assert "rate_limits_1.window_start" in delete_sql
    assert "rate_limits.source_digest !=" in delete_sql
    assert "rate_limits_1.source_digest !=" in delete_sql


def test_maintenance_cycle_deletes_rate_limits_in_batches_up_to_two_thousand(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    cutoff = now - timedelta(hours=24)
    digests = [f"{index:064x}" for index in range(2001)]

    with factory() as database:
        database.add_all(
            [
                RateLimit(
                    source_digest=digest,
                    window_start=cutoff - timedelta(seconds=2001 - index),
                    count=1,
                    updated_at=now,
                )
                for index, digest in enumerate(digests)
            ]
        )
        database.commit()

    result = run_maintenance_cycle(factory, now=now)

    with factory() as database:
        remaining_digests = tuple(database.scalars(select(RateLimit.source_digest)))

    assert result.obsolete_rate_limits_deleted == 2000
    assert remaining_digests == (digests[-1],)


def test_maintenance_never_deletes_the_global_capacity_lock_sentinel(
    app_harness: AppHarness,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    obsolete_digest = "d" * 64

    with factory() as database:
        database.add_all(
            [
                RateLimit(
                    source_digest=GLOBAL_CAPACITY_LOCK_DIGEST,
                    window_start=epoch,
                    count=0,
                    updated_at=now,
                ),
                RateLimit(
                    source_digest=obsolete_digest,
                    window_start=epoch,
                    count=1,
                    updated_at=now,
                ),
            ]
        )
        database.commit()

    result = run_maintenance_cycle(factory, now=now)

    with factory() as database:
        remaining_digests = tuple(database.scalars(select(RateLimit.source_digest)))

    assert result.obsolete_rate_limits_deleted == 1
    assert remaining_digests == (GLOBAL_CAPACITY_LOCK_DIGEST,)


def test_failed_later_batch_keeps_prior_commit_and_rolls_back_current_batch(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        database.add_all(
            [
                Organization(
                    id=f"failure-expired-{index:03d}",
                    name=f"Failure expired demo {index:03d}",
                    is_demo=True,
                    created_at=now - timedelta(days=1),
                    expires_at=now - timedelta(seconds=201 - index),
                )
                for index in range(201)
            ]
        )
        database.commit()

    real_delete_batch = maintenance.delete_expired_workspace_batch
    calls = 0

    def fail_second_batch(database: Session, *, now: datetime) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            database.execute(
                delete(Organization).where(
                    Organization.id == "failure-expired-100",
                )
            )
            raise RuntimeError("current batch must be rolled back")
        return real_delete_batch(database, now=now)

    monkeypatch.setattr(
        maintenance,
        "delete_expired_workspace_batch",
        fail_second_batch,
    )

    result = run_maintenance_cycle(factory, now=now)

    with factory() as database:
        remaining_ids = set(database.scalars(select(Organization.id)))

    assert result.expired_workspaces_deleted == 100
    assert result.failed_phase == "expired_workspaces"
    assert len(remaining_ids) == 101
    assert "failure-expired-100" in remaining_ids


def test_rate_limit_batch_failure_logs_only_safe_metadata(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = app_harness.clock()
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    cutoff = now - timedelta(hours=24)
    digests = [f"{index + 10_000:064x}" for index in range(501)]

    with factory() as database:
        database.add_all(
            [
                RateLimit(
                    source_digest=digest,
                    window_start=cutoff - timedelta(seconds=501 - index),
                    count=1,
                    updated_at=now,
                )
                for index, digest in enumerate(digests)
            ]
        )
        database.commit()

    class SensitiveMaintenanceError(RuntimeError):
        pass

    sensitive_marker = "SELECT secret_digest FROM private_payload"
    real_delete_batch = maintenance.delete_obsolete_rate_limit_batch
    calls = 0

    def fail_second_batch(database: Session, *, now: datetime) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            database.execute(
                delete(RateLimit).where(
                    RateLimit.source_digest == digests[-1],
                )
            )
            raise SensitiveMaintenanceError(sensitive_marker)
        return real_delete_batch(database, now=now)

    monkeypatch.setattr(
        maintenance,
        "delete_obsolete_rate_limit_batch",
        fail_second_batch,
    )
    caplog.set_level(logging.ERROR, logger=maintenance.__name__)

    result = run_maintenance_cycle(factory, now=now)

    with factory() as database:
        remaining_digests = tuple(database.scalars(select(RateLimit.source_digest)))

    assert result.obsolete_rate_limits_deleted == 500
    assert result.failed_phase == "obsolete_rate_limits"
    assert remaining_digests == (digests[-1],)
    assert len(caplog.records) == 1
    metadata = vars(caplog.records[0])
    assert metadata["phase"] == "obsolete_rate_limits"
    assert metadata["batch_number"] == 2
    assert metadata["row_count"] == 500
    assert metadata["exception_type"] == "SensitiveMaintenanceError"
    assert sensitive_marker not in caplog.text
    assert digests[-1] not in caplog.text


def test_scheduler_runs_immediately_then_waits_one_hour(
    app_harness: AppHarness,
) -> None:
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    cycle_times: list[datetime] = []
    wait_delays: list[float] = []

    def cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        assert _session_factory is factory
        cycle_times.append(now)

    async def stop_after_first_wait(
        _stop_event: asyncio.Event,
        delay_seconds: float,
    ) -> bool:
        wait_delays.append(delay_seconds)
        return True

    async def exercise() -> None:
        await run_maintenance_scheduler(
            session_factory=factory,
            clock=app_harness.clock,
            cycle=cycle,
            stop_event=asyncio.Event(),
            wait=stop_after_first_wait,
        )

    asyncio.run(exercise())

    assert cycle_times == [app_harness.clock()]
    assert wait_delays == [MAINTENANCE_INTERVAL_SECONDS]
    assert MAINTENANCE_INTERVAL_SECONDS == 3_600


def test_scheduler_runs_again_after_each_completed_interval(
    app_harness: AppHarness,
) -> None:
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    cycle_times: list[datetime] = []
    wait_calls = 0

    def cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        assert _session_factory is factory
        cycle_times.append(now)

    async def complete_one_interval_then_stop(
        _stop_event: asyncio.Event,
        delay_seconds: float,
    ) -> bool:
        nonlocal wait_calls
        assert delay_seconds == MAINTENANCE_INTERVAL_SECONDS
        wait_calls += 1
        if wait_calls == 1:
            app_harness.clock.advance(hours=1)
            return False
        return True

    async def exercise() -> None:
        await run_maintenance_scheduler(
            session_factory=factory,
            clock=app_harness.clock,
            cycle=cycle,
            stop_event=asyncio.Event(),
            wait=complete_one_interval_then_stop,
        )

    first_cycle_time = app_harness.clock()
    asyncio.run(exercise())

    assert cycle_times == [
        first_cycle_time,
        first_cycle_time + timedelta(hours=1),
    ]
    assert wait_calls == 2


def test_scheduler_continues_after_a_cycle_failure_without_logging_details(
    app_harness: AppHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    attempts = 0
    wait_calls = 0
    sensitive_marker = "sqlite:///private/path?token=secret"

    class SensitiveCycleError(RuntimeError):
        pass

    def cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        del now
        nonlocal attempts
        assert _session_factory is factory
        attempts += 1
        if attempts == 1:
            raise SensitiveCycleError(sensitive_marker)

    async def complete_one_interval_then_stop(
        _stop_event: asyncio.Event,
        _delay_seconds: float,
    ) -> bool:
        nonlocal wait_calls
        wait_calls += 1
        return wait_calls == 2

    async def exercise() -> None:
        await run_maintenance_scheduler(
            session_factory=factory,
            clock=app_harness.clock,
            cycle=cycle,
            stop_event=asyncio.Event(),
            wait=complete_one_interval_then_stop,
        )

    caplog.set_level(logging.WARNING, logger=maintenance.__name__)
    asyncio.run(exercise())

    assert attempts == 2
    assert wait_calls == 2
    assert len(caplog.records) == 1
    assert vars(caplog.records[0])["exception_type"] == "SensitiveCycleError"
    assert sensitive_marker not in caplog.text


def test_demo_lifespan_awaits_one_scheduler_before_disposing_the_engine(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="demo",
        database_url=app_harness.settings.database_url,
        session_secret=app_harness.settings.session_secret,
        allowed_hosts=("testserver",),
    )
    cycle_started = threading.Event()
    lifecycle_events: list[str] = []
    cycle_count = 0

    def cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        del _session_factory, now
        nonlocal cycle_count
        cycle_count += 1
        cycle_started.set()

    async def wait_for_shutdown(
        stop_event: asyncio.Event,
        delay_seconds: float,
    ) -> bool:
        assert delay_seconds == MAINTENANCE_INTERVAL_SECONDS
        await stop_event.wait()
        lifecycle_events.append("scheduler_stopped")
        return True

    application = create_app(
        settings,
        maintenance_cycle=cycle,
        maintenance_wait=wait_for_shutdown,
    )
    original_dispose = application.state.database_engine.dispose

    def tracked_dispose() -> None:
        lifecycle_events.append("engine_disposed")
        original_dispose()

    monkeypatch.setattr(application.state.database_engine, "dispose", tracked_dispose)

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        assert cycle_started.wait(timeout=5)

    assert cycle_count == 1
    assert lifecycle_events == ["scheduler_stopped", "engine_disposed"]


def test_maintenance_shutdown_timeout_is_loaded_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMERCE_OPS_MAINTENANCE_SHUTDOWN_TIMEOUT_SECONDS", "0.125")

    settings = Settings(_env_file=None)

    assert settings.maintenance_shutdown_timeout_seconds == 0.125


def test_maintenance_shutdown_timeout_defaults_to_five_seconds() -> None:
    settings = Settings(_env_file=None)

    assert settings.maintenance_shutdown_timeout_seconds == 5.0


@pytest.mark.parametrize("invalid_timeout", [0, -1, float("inf")])
def test_maintenance_shutdown_timeout_requires_a_finite_positive_value(
    invalid_timeout: float,
) -> None:
    with pytest.raises(ValueError, match="maintenance_shutdown_timeout_seconds"):
        Settings(
            _env_file=None,
            maintenance_shutdown_timeout_seconds=invalid_timeout,
        )


def test_demo_lifespan_bounds_shutdown_when_a_sync_cycle_is_still_running(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="demo",
        database_url=app_harness.settings.database_url,
        session_secret=app_harness.settings.session_secret,
        allowed_hosts=("testserver",),
        maintenance_shutdown_timeout_seconds=0.01,
    )
    cycle_started = threading.Event()
    allow_cycle_to_finish = threading.Event()
    cycle_finished = threading.Event()
    lifecycle_events: list[str] = []

    def blocked_cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        del _session_factory, now
        cycle_started.set()
        assert allow_cycle_to_finish.wait(timeout=5)
        cycle_finished.set()

    application = create_app(settings, maintenance_cycle=blocked_cycle)
    original_dispose = application.state.database_engine.dispose

    def tracked_dispose() -> None:
        lifecycle_events.append("engine_disposed")
        original_dispose()

    monkeypatch.setattr(application.state.database_engine, "dispose", tracked_dispose)

    async def exercise() -> tuple[bool, float]:
        lifespan = application.router.lifespan_context(application)
        await lifespan.__aenter__()
        for _attempt in range(2_000):
            if cycle_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert cycle_started.is_set()

        loop = asyncio.get_running_loop()
        shutdown_started = loop.time()
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        shutdown_was_bounded = True
        try:
            await asyncio.wait_for(asyncio.shield(shutdown), timeout=0.25)
        except TimeoutError:
            shutdown_was_bounded = False
        elapsed = loop.time() - shutdown_started

        allow_cycle_to_finish.set()
        await shutdown
        for _attempt in range(2_000):
            if cycle_finished.is_set():
                break
            await asyncio.sleep(0.001)
        assert cycle_finished.is_set()
        return shutdown_was_bounded, elapsed

    caplog.set_level(logging.WARNING, logger="app.main")
    shutdown_was_bounded, elapsed = asyncio.run(exercise())

    assert shutdown_was_bounded, f"lifespan shutdown remained blocked for {elapsed:.3f}s"
    assert lifecycle_events == ["engine_disposed"]
    assert len(caplog.records) == 1
    assert caplog.records[0].message == "maintenance_shutdown_timed_out"
    assert vars(caplog.records[0])["timeout_seconds"] == 0.01


@pytest.mark.parametrize(
    ("demo_mode", "webhook_enabled"),
    [
        pytest.param(True, False, id="demo-mode"),
        pytest.param(False, True, id="webhook"),
    ],
)
def test_production_lifespan_runs_maintenance_for_stateful_features(
    tmp_path: Path,
    demo_mode: bool,
    webhook_enabled: bool,
) -> None:
    database_path = tmp_path / "production-maintenance.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=f"sqlite+pysqlite:///{database_path}",
        session_secret="production-maintenance-session-secret-at-least-32-bytes",
        allowed_hosts=("testserver",),
        demo_mode=demo_mode,
        webhook_enabled=webhook_enabled,
        webhook_master_secret=(
            "production-maintenance-webhook-secret-at-least-32-bytes" if webhook_enabled else None
        ),
    )
    cycle_started = threading.Event()
    cycle_count = 0

    def cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        del _session_factory, now
        nonlocal cycle_count
        cycle_count += 1
        cycle_started.set()

    async def wait_for_shutdown(
        stop_event: asyncio.Event,
        _delay_seconds: float,
    ) -> bool:
        await stop_event.wait()
        return True

    application = create_app(
        settings,
        maintenance_cycle=cycle,
        maintenance_wait=wait_for_shutdown,
    )

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        assert cycle_started.wait(timeout=2)

    assert cycle_count == 1


def test_production_lifespan_skips_maintenance_without_stateful_features(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "production-no-maintenance.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=f"sqlite+pysqlite:///{database_path}",
        session_secret="production-no-maintenance-session-secret-at-least-32-bytes",
        allowed_hosts=("testserver",),
        demo_mode=False,
        webhook_enabled=False,
    )
    cycle_started = threading.Event()

    def cycle(
        _session_factory: sessionmaker[Session],
        *,
        now: datetime,
    ) -> None:
        del _session_factory, now
        cycle_started.set()

    application = create_app(settings, maintenance_cycle=cycle)

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        assert not cycle_started.wait(timeout=0.1)
