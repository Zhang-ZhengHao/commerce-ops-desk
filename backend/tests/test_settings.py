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
    assert settings.webhook_enabled is False
    assert settings.webhook_master_secret is None
    assert settings.webhook_source_minute_limit == 120
    assert settings.demo_webhook_event_limit == 20
    assert settings.api_max_request_body_bytes == 16 * 1024
    assert settings.trusted_proxy_cidrs == ()
    assert settings.allowed_hosts == ()
    assert settings.secure_cookies is False


def test_production_defaults_demo_off_and_requires_an_explicit_session_secret() -> None:
    with pytest.raises(ValidationError, match="session secret"):
        Settings(_env_file=None, environment="production")

    settings = Settings(
        _env_file=None,
        environment="production",
        session_secret="production-secret-that-is-long-enough-for-hmac",
        allowed_hosts=("127.0.0.1",),
    )

    assert settings.demo_mode is False
    assert settings.secure_cookies is True
    assert "production-secret" not in repr(settings)


def test_cookie_secure_defaults_are_safe_but_can_be_explicitly_overridden() -> None:
    demo_settings = Settings(
        _env_file=None,
        environment="demo",
        session_secret="demo-secret-that-is-long-enough-for-hmac-only",
        allowed_hosts=("127.0.0.1",),
    )
    local_http_settings = Settings(
        _env_file=None,
        environment="demo",
        session_secret="demo-secret-that-is-long-enough-for-hmac-only",
        cookie_secure=False,
        allowed_hosts=("127.0.0.1",),
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


def test_enabled_webhook_requires_an_independent_secret_of_at_least_32_bytes() -> None:
    with pytest.raises(ValidationError, match="webhook master secret"):
        Settings(_env_file=None, webhook_enabled=True)

    short_multibyte_secret = "密" * 10
    assert len(short_multibyte_secret) == 10
    assert len(short_multibyte_secret.encode("utf-8")) == 30
    with pytest.raises(ValidationError, match="webhook master secret") as short_error:
        Settings(
            _env_file=None,
            webhook_enabled=True,
            webhook_master_secret=short_multibyte_secret,
        )
    assert short_multibyte_secret not in str(short_error.value)

    webhook_secret = "webhook-secret-that-is-independent-and-long-enough"
    settings = Settings(
        _env_file=None,
        webhook_enabled=True,
        webhook_master_secret=webhook_secret,
    )

    assert settings.webhook_master_secret is not None
    assert settings.webhook_master_secret.get_secret_value() == webhook_secret
    assert webhook_secret not in repr(settings)

    reused_secret = "one-secret-must-not-authenticate-browser-and-webhook"
    with pytest.raises(ValidationError, match="must be independent") as error:
        Settings(
            _env_file=None,
            session_secret=reused_secret,
            webhook_enabled=True,
            webhook_master_secret=reused_secret,
        )
    assert reused_secret not in str(error.value)


def test_webhook_limits_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, webhook_source_minute_limit=0)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, demo_webhook_event_limit=0)


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


def test_allowed_hosts_are_exact_normalized_hostnames() -> None:
    settings = Settings(
        _env_file=None,
        environment="demo",
        session_secret="demo-secret-that-is-long-enough-for-hmac-only",
        allowed_hosts=(
            "Commerce-Ops-Desk.SRRSH.AIG.REST",
            "localhost",
            "127.0.0.1",
            "api.example.com",
            "localhost",
        ),
    )

    assert settings.allowed_hosts == (
        "commerce-ops-desk.srrsh.aig.rest",
        "localhost",
        "127.0.0.1",
        "api.example.com",
    )


@pytest.mark.parametrize(
    "invalid_host",
    [
        "",
        "*",
        "*.example.com",
        "https://example.com",
        "example.com/path",
        "example.com:443",
        ".example.com",
        "example..com",
        "-example.com",
        "example-.com",
        f"{'a' * 64}.example.com",
        f"{'a' * 250}.com",
        " example.com",
        "example.com?query=yes",
        "example.com#fragment",
    ],
)
def test_allowed_hosts_reject_ambiguous_or_wildcard_values(invalid_host: str) -> None:
    with pytest.raises(ValidationError, match="allowed host"):
        Settings(
            _env_file=None,
            environment="test",
            allowed_hosts=(invalid_host,),
        )


@pytest.mark.parametrize("environment", ["demo", "production"])
def test_hardened_environments_require_allowed_hosts(environment: str) -> None:
    with pytest.raises(ValidationError, match="allowed host"):
        Settings(
            _env_file=None,
            environment=environment,
            session_secret="hardened-secret-that-is-long-enough-for-hmac",
        )


def test_allowed_hosts_read_from_the_namespaced_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "COMMERCE_OPS_ALLOWED_HOSTS",
        '["commerce-ops-desk.srrsh.aig.rest", "127.0.0.1"]',
    )

    settings = Settings(_env_file=None, environment="test")

    assert settings.allowed_hosts == (
        "commerce-ops-desk.srrsh.aig.rest",
        "127.0.0.1",
    )
