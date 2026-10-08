from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import quote, quote_plus
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import Engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DATABASE_NAME_PREFIX = "commerce_ops_test_"
POSTGRES_ADMIN_URL_ENV = "COMMERCE_OPS_POSTGRES_TEST_ADMIN_URL"


class PostgresHarnessConfigurationError(ValueError):
    """Raised when the dedicated PostgreSQL test boundary is not configured safely."""


class PostgresHarnessError(RuntimeError):
    """Raised when a harness operation fails without exposing connection credentials."""


EngineType = TypeVar("EngineType", bound=Engine)


@dataclass(frozen=True)
class TemporaryPostgresDatabase:
    name: str
    url: URL = field(repr=False)
    _engines: list[Engine] = field(default_factory=list, init=False, repr=False)

    def register_engine(self, engine: EngineType) -> EngineType:
        """Register an engine so teardown disposes its pool before dropping the database."""
        self._engines.append(engine)
        return engine

    def dispose_registered_engines(self) -> None:
        dispose_failed = False
        while self._engines:
            engine = self._engines.pop()
            try:
                engine.dispose()
            except Exception:
                dispose_failed = True
        if dispose_failed:
            raise PostgresHarnessError(
                "Could not dispose a PostgreSQL test engine during cleanup"
            ) from None


def load_postgres_admin_url(environ: Mapping[str, str] | None = None) -> str:
    """Read only the dedicated test administrator URL and reject ambiguous whitespace."""
    source = os.environ if environ is None else environ
    configured = source.get(POSTGRES_ADMIN_URL_ENV)
    if not configured:
        raise PostgresHarnessConfigurationError(
            f"{POSTGRES_ADMIN_URL_ENV} must be set for PostgreSQL tests"
        )
    if configured != configured.strip():
        raise PostgresHarnessConfigurationError(
            f"{POSTGRES_ADMIN_URL_ENV} must not contain surrounding whitespace"
        )
    _parse_admin_url(configured)
    return configured


def _parse_admin_url(admin_url: str) -> URL:
    try:
        parsed = make_url(admin_url)
    except (ArgumentError, TypeError, ValueError):
        raise PostgresHarnessConfigurationError(
            "PostgreSQL test administrator URL is invalid"
        ) from None

    if parsed.drivername.split("+", maxsplit=1)[0] != "postgresql":
        raise PostgresHarnessConfigurationError(
            "PostgreSQL test administrator URL must select PostgreSQL"
        )
    if not parsed.database:
        raise PostgresHarnessConfigurationError(
            "PostgreSQL test administrator URL must name an administrative database"
        )
    if any(_is_credential_query_key(key) for key in parsed.query):
        raise PostgresHarnessConfigurationError(
            "PostgreSQL test administrator URL must not place credentials in query parameters"
        )
    return parsed


def _is_credential_query_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(
        marker in normalized for marker in ("password", "passfile", "secret", "token", "credential")
    )


def _psycopg_dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def redact_database_credentials(output: str, database_url: str | URL) -> str:
    """Remove full URLs and raw or URL-encoded passwords from diagnostic text."""
    try:
        parsed = make_url(database_url) if isinstance(database_url, str) else database_url
    except (ArgumentError, TypeError, ValueError):
        return "[REDACTED DATABASE DIAGNOSTIC]"

    if any(_is_credential_query_key(key) for key in parsed.query):
        return "[REDACTED DATABASE DIAGNOSTIC]"

    redacted = output
    unsafe_url = parsed.render_as_string(hide_password=False)
    safe_url = parsed.render_as_string(hide_password=True)
    redacted = redacted.replace(unsafe_url, safe_url)
    if isinstance(database_url, str):
        redacted = redacted.replace(database_url, safe_url)

    password = parsed.password
    if password:
        for candidate in (
            password,
            quote(password, safe=""),
            quote_plus(password, safe=""),
        ):
            redacted = redacted.replace(candidate, "[REDACTED]")
    return redacted


def _create_database(admin_url: URL, database_name: str) -> None:
    try:
        with psycopg.connect(_psycopg_dsn(admin_url), autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    except Exception:
        raise PostgresHarnessError(
            "Could not create the temporary PostgreSQL test database"
        ) from None


def _drop_database(admin_url: URL, database_name: str) -> None:
    try:
        with psycopg.connect(_psycopg_dsn(admin_url), autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
    except Exception:
        raise PostgresHarnessError(
            "Could not clean up the temporary PostgreSQL test database"
        ) from None


@contextmanager
def temporary_postgres_database(admin_url: str) -> Iterator[TemporaryPostgresDatabase]:
    parsed_admin_url = _parse_admin_url(admin_url)
    database_name = f"{DATABASE_NAME_PREFIX}{uuid4().hex}"
    database_url = parsed_admin_url.set(
        database=database_name,
        drivername="postgresql+psycopg",
    )

    _create_database(parsed_admin_url, database_name)
    database = TemporaryPostgresDatabase(name=database_name, url=database_url)

    try:
        yield database
    finally:
        dispose_failed = False
        try:
            database.dispose_registered_engines()
        except Exception:
            dispose_failed = True

        _drop_database(parsed_admin_url, database_name)
        if dispose_failed:
            raise PostgresHarnessError(
                "Could not dispose a PostgreSQL test engine during cleanup"
            ) from None
