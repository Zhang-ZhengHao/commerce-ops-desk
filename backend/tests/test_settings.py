from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.database import build_engine


def test_settings_default_to_the_local_sqlite_boundary() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "CommerceOps Desk"
    assert settings.environment == "development"
    assert settings.database_backend == "sqlite"
    assert settings.database_url.get_secret_value() == ("sqlite+pysqlite:///./data/commerce_ops.db")


def test_settings_read_only_namespaced_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://ignored.invalid/example")
    monkeypatch.setenv(
        "COMMERCE_OPS_DATABASE_URL",
        "postgresql://demo_user:demo_password@db.invalid/commerce_ops",
    )
    monkeypatch.setenv("COMMERCE_OPS_ENVIRONMENT", "test")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.database_backend == "postgresql"
    assert settings.database_url.get_secret_value() == (
        "postgresql+psycopg://demo_user:demo_password@db.invalid/commerce_ops"
    )
    assert "demo_password" not in repr(settings)


def test_settings_reject_unsupported_database_drivers() -> None:
    with pytest.raises(ValidationError, match="SQLite or PostgreSQL"):
        Settings(
            _env_file=None,
            database_url="mysql+pymysql://demo_user:demo_password@db.invalid/commerce_ops",
        )


def test_sqlite_engine_enforces_demo_safety_pragmas(tmp_path: Path) -> None:
    database_path = tmp_path / "commerce-ops.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+pysqlite:///{database_path}",
    )

    engine = build_engine(settings)
    try:
        with engine.connect() as connection:
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()

        assert engine.dialect.name == "sqlite"
        assert engine.dialect.driver == "pysqlite"
        assert foreign_keys == 1
        assert busy_timeout == 5_000
        assert journal_mode == "wal"
    finally:
        engine.dispose()


def test_postgresql_engine_uses_the_psycopg_driver_without_connecting() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql://demo_user:demo_password@db.invalid/commerce_ops",
    )

    engine = build_engine(settings)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.url.drivername == "postgresql+psycopg"
        assert "demo_password" not in str(engine.url)
    finally:
        engine.dispose()
