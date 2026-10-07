"""Synchronous SQLAlchemy engine construction and readiness probing."""

from typing import Any

from sqlalchemy import Engine, create_engine, event, text

from app.config import Settings

SQLITE_BUSY_TIMEOUT_MS = 5_000


def build_engine(settings: Settings) -> Engine:
    """Build a lazy engine for the configured supported database."""
    database_url = settings.database_url.get_secret_value()
    engine_options: dict[str, Any] = {"pool_pre_ping": True}

    if settings.database_backend == "sqlite":
        engine_options["connect_args"] = {
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1_000,
        }

    engine = create_engine(database_url, **engine_options)

    if settings.database_backend == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def probe_database(engine: Engine) -> None:
    """Raise when the database cannot execute the smallest useful query."""
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1")).scalar_one()
        if result != 1:
            raise RuntimeError("Database readiness probe returned an unexpected value")
