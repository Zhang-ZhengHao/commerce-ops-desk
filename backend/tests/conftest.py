"""Deterministic integration fixtures for the demo identity boundary."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"
SAME_ORIGIN = "http://commerceops.test"
FROZEN_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
TEST_SESSION_SECRET = "i02-test-only-session-secret-with-at-least-32-bytes"


@dataclass
class FrozenClock:
    """A mutable UTC clock; tests advance it without sleeping."""

    current: datetime = FROZEN_NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


@dataclass
class DeterministicTokenFactory:
    """Issue unique, inspectable test tokens without system randomness."""

    prefix: str = "i02-deterministic-token"
    issued: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __call__(self) -> str:
        with self._lock:
            token = f"{self.prefix}-{len(self.issued) + 1:04d}"
            self.issued.append(token)
            return token


@dataclass(frozen=True)
class AppHarness:
    app: FastAPI
    database_path: Path
    settings: Settings
    clock: FrozenClock
    token_factory: DeterministicTokenFactory

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


def _upgrade_database(database_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "COMMERCE_OPS_DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
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
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def app_harness_factory(
    tmp_path: Path,
) -> Callable[..., AppHarness]:
    """Build isolated migrated apps while keeping time and entropy injectable."""

    sequence = 0

    def build(
        *,
        database_path: Path | None = None,
        clock: FrozenClock | None = None,
        token_factory: DeterministicTokenFactory | None = None,
        migrate: bool = True,
        **setting_overrides: Any,
    ) -> AppHarness:
        nonlocal sequence
        sequence += 1
        resolved_database_path = database_path or tmp_path / f"i02-{sequence}.sqlite3"
        if migrate and not resolved_database_path.exists():
            _upgrade_database(resolved_database_path)

        resolved_clock = clock or FrozenClock()
        resolved_token_factory = token_factory or DeterministicTokenFactory(
            prefix=f"i02-token-{sequence}"
        )
        setting_values: dict[str, Any] = {
            "environment": "test",
            "database_url": f"sqlite+pysqlite:///{resolved_database_path}",
            "session_secret": TEST_SESSION_SECRET,
            "demo_source_hourly_limit": 10,
            "demo_active_workspace_limit": 500,
            "demo_workspace_ttl_hours": 4,
            "trusted_proxy_cidrs": (),
        }
        setting_values.update(setting_overrides)
        settings = Settings(_env_file=None, **setting_values)

        # During RED the I01 factory does not yet accept these injection seams.
        # Falling back keeps failures focused on the missing HTTP behavior; the
        # exact expiry/token assertions still require the seams to be wired.
        factory_parameters = inspect.signature(create_app).parameters
        factory_arguments: dict[str, Any] = {}
        if "clock" in factory_parameters:
            factory_arguments["clock"] = resolved_clock
        if "token_factory" in factory_parameters:
            factory_arguments["token_factory"] = resolved_token_factory

        app = create_app(settings, **factory_arguments)
        return AppHarness(
            app=app,
            database_path=resolved_database_path,
            settings=settings,
            clock=resolved_clock,
            token_factory=resolved_token_factory,
        )

    return build


@pytest.fixture
def app_harness(app_harness_factory: Callable[..., AppHarness]) -> AppHarness:
    return app_harness_factory()


@pytest.fixture
def client(app_harness: AppHarness) -> Generator[TestClient, None, None]:
    with app_harness.client() as test_client:
        yield test_client


@pytest.fixture
def bootstrap_workspace() -> Callable[..., Any]:
    key_sequence = count(1)

    def bootstrap(
        client: TestClient,
        *,
        role: str = "manager",
        origin: str = SAME_ORIGIN,
        idempotency_key: str | None = None,
    ) -> Any:
        resolved_key = idempotency_key or f"fixture-bootstrap-{next(key_sequence)}"
        return client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": origin,
                "Idempotency-Key": resolved_key,
            },
            json={"initial_role": role},
        )

    return bootstrap
