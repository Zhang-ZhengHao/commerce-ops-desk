"""Pre-authentication controls for the public webhook ingress boundary."""

import hmac
import math
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.repositories.rate_limits import claim_fixed_window_slot

WEBHOOK_SOURCE_DIGEST_CONTEXT: Final = b"commerce-ops:webhook-source:v1\n"


class WebhookIngressRateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("webhook ingress source limit exceeded")
        self.retry_after = retry_after


def derive_webhook_source_digest(
    webhook_master_secret: bytes,
    *,
    normalized_source: str,
) -> str:
    """Return the keyed pseudonym persisted for one normalized request source."""

    if len(webhook_master_secret) < 32:
        raise ValueError("webhook master secret must contain at least 32 bytes")
    try:
        source_bytes = normalized_source.encode("utf-8")
    except UnicodeEncodeError:
        source_bytes = None
    if source_bytes is None:
        raise ValueError("normalized webhook source must be valid UTF-8")
    return hmac.digest(
        webhook_master_secret,
        WEBHOOK_SOURCE_DIGEST_CONTEXT + source_bytes,
        "sha256",
    ).hex()


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("webhook clock must be timezone-aware")
    return now.astimezone(UTC)


def _fixed_minute(now: datetime) -> datetime:
    return _as_utc(now).replace(second=0, microsecond=0)


def _retry_after(now: datetime) -> int:
    normalized_now = _as_utc(now)
    window_end = _fixed_minute(normalized_now) + timedelta(minutes=1)
    return max(1, math.ceil((window_end - normalized_now).total_seconds()))


def enforce_webhook_source_limit(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    normalized_source: str,
    now: datetime,
) -> None:
    """Commit one pre-authentication attempt in an isolated short transaction."""

    secret = settings.webhook_master_secret
    if secret is None:
        raise RuntimeError("webhook master secret is unavailable")
    normalized_now = _as_utc(now)
    source_digest = derive_webhook_source_digest(
        secret.get_secret_value().encode("utf-8"),
        normalized_source=normalized_source,
    )
    with session_factory.begin() as database:
        admitted = claim_fixed_window_slot(
            database,
            source_digest=source_digest,
            window_start=_fixed_minute(normalized_now),
            now=normalized_now,
            maximum=settings.webhook_source_minute_limit,
        )

    if not admitted:
        raise WebhookIngressRateLimitExceeded(_retry_after(normalized_now))
