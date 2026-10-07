"""Runtime configuration with an explicit database-driver boundary."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

Environment = Literal["development", "test", "demo", "production"]


class Settings(BaseSettings):
    """Application settings loaded only from the CommerceOps namespace."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMMERCE_OPS_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = "CommerceOps Desk"
    environment: Environment = "development"
    database_url: SecretStr = SecretStr("sqlite+pysqlite:///./data/commerce_ops.db")

    @field_validator("database_url", mode="before")
    @classmethod
    def validate_database_url(cls, value: object) -> SecretStr:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw_value, str):
            raise ValueError("Database URL must select SQLite or PostgreSQL")

        try:
            url = make_url(raw_value)
        except ArgumentError:
            raise ValueError("Database URL must select SQLite or PostgreSQL") from None

        if url.drivername in {"sqlite", "sqlite+pysqlite"}:
            normalized = url.set(drivername="sqlite+pysqlite")
        elif url.drivername in {"postgresql", "postgresql+psycopg"}:
            normalized = url.set(drivername="postgresql+psycopg")
        else:
            raise ValueError("Database URL must select SQLite or PostgreSQL")

        return SecretStr(normalized.render_as_string(hide_password=False))

    @property
    def database_backend(self) -> Literal["sqlite", "postgresql"]:
        backend = make_url(self.database_url.get_secret_value()).get_backend_name()
        if backend == "sqlite":
            return "sqlite"
        return "postgresql"
