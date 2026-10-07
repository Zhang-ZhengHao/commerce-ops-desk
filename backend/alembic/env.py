"""Alembic environment wired to the application's validated database boundary."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app import models as _models  # noqa: F401
from app.config import Settings
from app.database import Base, build_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Return the normalized URL without placing credentials in alembic.ini."""
    return Settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Render migrations without opening a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations through the same engine policy as the application."""
    engine = build_engine(Settings())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=connection.dialect.name == "sqlite",
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
