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
    assert settings.demo_mode is True
    assert settings.demo_source_hourly_limit == 10
    assert settings.demo_active_workspace_limit == 500
    assert settings.demo_workspace_ttl_hours == 4
    assert settings.demo_role_write_limit == 32
    assert settings.demo_case_note_limit == 200
    assert settings.api_max_request_body_bytes == 16 * 1024
    assert settings.trusted_proxy_cidrs == ()
    assert settings.secure_cookies is False


def test_production_defaults_demo_off_and_requires_an_explicit_session_secret() -> None:
    with pytest.raises(ValidationError, match="session secret"):
        Settings(_env_file=None, environment="production")

    settings = Settings(
        _env_file=None,
        environment="production",
        session_secret="production-secret-that-is-long-enough-for-hmac",
    )

    assert settings.demo_mode is False
    assert settings.secure_cookies is True
    assert "production-secret" not in repr(settings)


def test_cookie_secure_defaults_are_safe_but_can_be_explicitly_overridden() -> None:
    demo_settings = Settings(
        _env_file=None,
        environment="demo",
        session_secret="demo-secret-that-is-long-enough-for-hmac-only",
    )
    local_http_settings = Settings(
        _env_file=None,
        environment="demo",
        session_secret="demo-secret-that-is-long-enough-for-hmac-only",
        cookie_secure=False,
    )

    assert demo_settings.secure_cookies is True
    assert local_http_settings.secure_cookies is False


def test_demo_limits_and_trusted_proxy_networks_are_validated() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        session_secret="test-secret-that-is-long-enough-for-hmac-only",
        demo_source_hourly_limit=3,
        demo_active_workspace_limit=7,
        demo_role_write_limit=5,
        demo_case_note_limit=11,
        api_max_request_body_bytes=2 * 1024,
        trusted_proxy_cidrs=("192.0.2.0/24", "2001:db8::/32"),
    )

    assert settings.demo_source_hourly_limit == 3
    assert settings.demo_active_workspace_limit == 7
    assert settings.demo_role_write_limit == 5
    assert settings.demo_case_note_limit == 11
    assert settings.api_max_request_body_bytes == 2 * 1024
    assert tuple(str(network) for network in settings.trusted_proxy_networks) == (
        "192.0.2.0/24",
        "2001:db8::/32",
    )

    with pytest.raises(ValidationError, match="valid CIDR"):
        Settings(
            _env_file=None,
            environment="test",
            session_secret="test-secret-that-is-long-enough-for-hmac-only",
            trusted_proxy_cidrs=("not-a-network",),
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="test",
            session_secret="test-secret-that-is-long-enough-for-hmac-only",
            api_max_request_body_bytes=0,
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="test",
            session_secret="test-secret-that-is-long-enough-for-hmac-only",
            demo_role_write_limit=0,
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="test",
            session_secret="test-secret-that-is-long-enough-for-hmac-only",
            demo_case_note_limit=0,
        )


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
