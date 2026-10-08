from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.config import Settings
from app.main import create_app
from postgres_tests.harness import (
    TemporaryPostgresDatabase,
    load_postgres_admin_url,
    redact_database_credentials,
    temporary_postgres_database,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"
SAME_ORIGIN = "http://commerceops.test"
FROZEN_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
POSTGRES_TEST_SESSION_SECRET = "postgres-test-session-secret-with-at-least-32-bytes"


@dataclass
class FrozenClock:
    current: datetime = FROZEN_NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


@dataclass
class DeterministicTokenFactory:
    prefix: str = "postgres-test-token"
    issued: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __call__(self) -> str:
        with self._lock:
            token = f"{self.prefix}-{len(self.issued) + 1:04d}"
            self.issued.append(token)
            return token


@dataclass(frozen=True)
class PostgresAppHarness:
    app: FastAPI
    engine: Engine
    settings: Settings
    clock: FrozenClock
    token_factory: DeterministicTokenFactory
    database: TemporaryPostgresDatabase

    def client(
        self,
        *,
        source_ip: str = "testclient",
        raise_server_exceptions: bool = True,
    ) -> TestClient:
        return TestClient(
            self.app,
            base_url=SAME_ORIGIN,
            client=(source_ip, 50_000),
            raise_server_exceptions=raise_server_exceptions,
        )


def _upgrade_postgres_database(database: TemporaryPostgresDatabase) -> None:
    database_url = database.url.render_as_string(hide_password=False)
    environment = os.environ.copy()
    environment.update(
        {
            "COMMERCE_OPS_DATABASE_URL": database_url,
            "COMMERCE_OPS_ENVIRONMENT": "test",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ALEMBIC_CONFIG),
            "upgrade",
            "head",
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = redact_database_credentials(
            result.stdout + result.stderr,
            database.url,
        )
        raise AssertionError(diagnostic) from None


@pytest.fixture(scope="session")
def postgres_admin_url() -> str:
    try:
        admin_url = load_postgres_admin_url()
    except ValueError as error:
        error_message = str(error)
    else:
        return admin_url

    pytest.fail(error_message, pytrace=False)


@pytest.fixture
def postgres_database(postgres_admin_url: str) -> Iterator[TemporaryPostgresDatabase]:
    with temporary_postgres_database(postgres_admin_url) as database:
        yield database


@pytest.fixture
def postgres_engine(postgres_database: TemporaryPostgresDatabase) -> Engine:
    engine = create_engine(postgres_database.url, pool_pre_ping=True)
    return postgres_database.register_engine(engine)


@pytest.fixture
def postgres_app_harness_factory(
    postgres_database: TemporaryPostgresDatabase,
) -> Callable[..., PostgresAppHarness]:
    sequence = 0
    migrated = False

    def build(
        *,
        clock: FrozenClock | None = None,
        token_factory: DeterministicTokenFactory | None = None,
        migrate: bool = True,
        **setting_overrides: Any,
    ) -> PostgresAppHarness:
        nonlocal migrated, sequence
        sequence += 1
        if migrate and not migrated:
            _upgrade_postgres_database(postgres_database)
            migrated = True

        resolved_clock = clock or FrozenClock()
        resolved_token_factory = token_factory or DeterministicTokenFactory(
            prefix=f"postgres-test-token-{sequence}"
        )
        setting_values: dict[str, Any] = {
            "environment": "test",
            "database_url": postgres_database.url.render_as_string(hide_password=False),
            "session_secret": POSTGRES_TEST_SESSION_SECRET,
            "demo_source_hourly_limit": 10,
            "demo_active_workspace_limit": 500,
            "demo_workspace_ttl_hours": 4,
            "trusted_proxy_cidrs": (),
        }
        setting_values.update(setting_overrides)
        settings = Settings(_env_file=None, **setting_values)
        app = create_app(
            settings,
            clock=resolved_clock,
            token_factory=resolved_token_factory,
        )
        engine = postgres_database.register_engine(cast(Engine, app.state.database_engine))
        return PostgresAppHarness(
            app=app,
            engine=engine,
            settings=settings,
            clock=resolved_clock,
            token_factory=resolved_token_factory,
            database=postgres_database,
        )

    return build


@pytest.fixture
def postgres_app_harness(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> PostgresAppHarness:
    return postgres_app_harness_factory()
