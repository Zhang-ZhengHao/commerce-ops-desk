from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import quote

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from postgres_tests.harness import (
    POSTGRES_ADMIN_URL_ENV,
    PostgresHarnessConfigurationError,
    PostgresHarnessError,
    load_postgres_admin_url,
    redact_database_credentials,
    temporary_postgres_database,
)


def _database_exists(admin_url: str, database_name: str) -> bool:
    psycopg_dsn = (
        make_url(admin_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    with psycopg.connect(psycopg_dsn, autocommit=True) as connection:
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
            (database_name,),
        ).fetchone()

    assert row is not None
    return bool(row[0])


def test_temporary_database_exists_only_inside_context(postgres_admin_url: str) -> None:
    with temporary_postgres_database(postgres_admin_url) as database:
        assert re.fullmatch(r"commerce_ops_test_[0-9a-f]{32}", database.name)
        assert database.url.database == database.name
        assert database.url.get_backend_name() == "postgresql"
        assert _database_exists(postgres_admin_url, database.name)

    assert not _database_exists(postgres_admin_url, database.name)


def test_cleanup_runs_when_test_body_raises(postgres_admin_url: str) -> None:
    database_name = ""
    checked_out_connection = None

    with (
        pytest.raises(RuntimeError, match="sentinel test failure"),
        temporary_postgres_database(postgres_admin_url) as database,
    ):
        database_name = database.name
        engine = database.register_engine(create_engine(database.url, pool_pre_ping=True))
        checked_out_connection = engine.connect()
        assert checked_out_connection.scalar(text("SELECT 1")) == 1
        raise RuntimeError("sentinel test failure")

    assert database_name
    assert not _database_exists(postgres_admin_url, database_name)
    assert checked_out_connection is not None
    with pytest.raises(SQLAlchemyError):
        checked_out_connection.close()


@pytest.mark.parametrize(
    "admin_url",
    (
        "sqlite+pysqlite:///not-postgres.sqlite3",
        "mysql+pymysql://user:password@database.invalid/admin",
        "not a database URL",
    ),
)
def test_non_postgresql_admin_url_fails_closed(admin_url: str) -> None:
    with (
        pytest.raises(PostgresHarnessConfigurationError, match="PostgreSQL"),
        temporary_postgres_database(admin_url),
    ):
        raise AssertionError("invalid URL must fail before the context body")


def test_missing_admin_url_fails_instead_of_skipping() -> None:
    with pytest.raises(
        PostgresHarnessConfigurationError,
        match=POSTGRES_ADMIN_URL_ENV,
    ):
        load_postgres_admin_url({})


def test_admin_url_whitespace_is_rejected_without_echoing_input() -> None:
    password = "harness-password-must-not-leak"
    configured = {
        POSTGRES_ADMIN_URL_ENV: (
            f" postgresql+psycopg://commerce_ops_ci:{password}@127.0.0.1/postgres"
        )
    }

    with pytest.raises(PostgresHarnessConfigurationError) as captured:
        load_postgres_admin_url(configured)

    assert password not in str(captured.value)


def test_connection_failures_do_not_expose_database_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "connection-password-must-not-leak"

    def fail_to_connect(*_args: object, **_kwargs: object) -> Iterator[object]:
        raise RuntimeError(f"driver included {password} in its error")

    monkeypatch.setattr(psycopg, "connect", fail_to_connect)

    with (
        pytest.raises(PostgresHarnessError) as captured,
        temporary_postgres_database(
            f"postgresql+psycopg://user:{password}@database.invalid/postgres"
        ),
    ):
        raise AssertionError("connection failure must prevent the context body")

    assert password not in str(captured.value)
    assert captured.value.__suppress_context__ is True


def test_database_credentials_are_redacted_from_subprocess_output() -> None:
    password = "marker:with/@characters"
    encoded_password = quote(password, safe="")
    database_url = (
        f"postgresql+psycopg://commerce_ops_ci:{encoded_password}@127.0.0.1:5432/postgres"
    )
    leaked_output = f"raw={password}\nencoded={encoded_password}\nurl={database_url}"

    redacted = redact_database_credentials(leaked_output, database_url)

    assert password not in redacted
    assert encoded_password not in redacted
    assert database_url not in redacted
    assert "[REDACTED]" in redacted
