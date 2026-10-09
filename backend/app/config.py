"""Runtime configuration with explicit database and public-demo boundaries."""

import re
import secrets
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

Environment = Literal["development", "test", "demo", "production"]
IPNetwork = IPv4Network | IPv6Network
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


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
    demo_mode: bool | None = None
    session_secret: SecretStr | None = None
    webhook_enabled: bool = False
    webhook_master_secret: SecretStr | None = None
    cookie_secure: bool | None = None
    demo_source_hourly_limit: int = Field(default=10, gt=0)
    demo_active_workspace_limit: int = Field(default=500, gt=0)
    demo_workspace_ttl_hours: int = Field(default=4, gt=0, le=24)
    demo_role_write_limit: int = Field(default=32, gt=0)
    demo_case_note_limit: int = Field(default=200, gt=0)
    webhook_source_minute_limit: int = Field(default=120, gt=0)
    demo_webhook_event_limit: int = Field(default=20, gt=0)
    api_max_request_body_bytes: int = Field(default=16 * 1024, gt=0)
    maintenance_shutdown_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        allow_inf_nan=False,
    )
    trusted_proxy_cidrs: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()

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

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode()) < 32:
            raise ValueError("session secret must contain at least 32 bytes")
        return value

    @field_validator("webhook_master_secret")
    @classmethod
    def validate_webhook_master_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("webhook master secret must contain at least 32 bytes")
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            for cidr in value:
                ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError("trusted proxy entries must be valid CIDR networks") from None
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized_hosts: list[str] = []
        for raw_host in value:
            if raw_host != raw_host.strip() or not raw_host:
                raise ValueError("allowed host must be an exact hostname or IPv4 address")

            host = raw_host.lower()
            try:
                address = ip_address(host)
            except ValueError:
                labels = host.split(".")
                if len(host) > 253 or any(HOST_LABEL.fullmatch(label) is None for label in labels):
                    raise ValueError(
                        "allowed host must be an exact hostname or IPv4 address"
                    ) from None
            else:
                if address.version != 4:
                    raise ValueError("allowed host must be an exact hostname or IPv4 address")
                host = address.compressed

            if host not in normalized_hosts:
                normalized_hosts.append(host)

        return tuple(normalized_hosts)

    @model_validator(mode="after")
    def resolve_security_defaults(self) -> "Settings":
        if self.demo_mode is None:
            object.__setattr__(self, "demo_mode", self.environment != "production")

        if self.session_secret is None:
            if self.environment == "production":
                raise ValueError("production requires an explicit session secret")
            object.__setattr__(self, "session_secret", SecretStr(secrets.token_urlsafe(48)))

        if self.webhook_enabled and self.webhook_master_secret is None:
            raise ValueError("enabled webhook requires a webhook master secret")

        if self.environment in {"demo", "production"} and not self.allowed_hosts:
            raise ValueError("demo and production require at least one allowed host")

        session_secret = self.session_secret
        if (
            self.webhook_master_secret is not None
            and session_secret is not None
            and secrets.compare_digest(
                session_secret.get_secret_value().encode("utf-8"),
                self.webhook_master_secret.get_secret_value().encode("utf-8"),
            )
        ):
            raise ValueError("webhook master secret must be independent")

        return self

    @property
    def database_backend(self) -> Literal["sqlite", "postgresql"]:
        backend = make_url(self.database_url.get_secret_value()).get_backend_name()
        if backend == "sqlite":
            return "sqlite"
        return "postgresql"

    @property
    def secure_cookies(self) -> bool:
        """Require HTTPS-only cookies in public demo and production environments."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment in {"demo", "production"}

    @property
    def trusted_proxy_networks(self) -> tuple[IPNetwork, ...]:
        """Return validated networks used only for the direct proxy hop."""
        return tuple(ip_network(cidr, strict=False) for cidr in self.trusted_proxy_cidrs)
