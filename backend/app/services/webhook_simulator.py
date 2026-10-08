"""Server-owned signed envelopes for the synthetic provider demo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.webhook import derive_webhook_integration_key, sign_webhook_request
from app.config import Settings
from app.models import Organization, WebhookIntegration

WebhookScenario = Literal["fresh", "stale"]


class WebhookSimulatorUnavailable(Exception):
    """The authenticated workspace has no active synthetic target."""


@dataclass(frozen=True, repr=False)
class SignedWebhookEnvelope:
    path: str
    body: str
    timestamp: str
    event_id: str
    signature: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("webhook simulator clock must be timezone-aware")
    return value.astimezone(UTC)


def _active_target(
    db: Session,
    *,
    organization_id: str,
    now: datetime,
) -> tuple[str, int] | None:
    row = db.execute(
        select(WebhookIntegration.id, WebhookIntegration.key_version)
        .join(
            Organization,
            Organization.id == WebhookIntegration.organization_id,
        )
        .where(
            WebhookIntegration.organization_id == organization_id,
            WebhookIntegration.provider == "synthetic",
            WebhookIntegration.enabled.is_(True),
            Organization.id == organization_id,
            Organization.is_demo.is_(True),
            Organization.expires_at > now,
        )
    ).one_or_none()
    if row is None:
        return None
    integration_id, key_version = row
    return integration_id, key_version


def build_signed_webhook_envelope(
    db: Session,
    *,
    organization_id: str,
    settings: Settings,
    now: datetime,
    scenario: WebhookScenario,
) -> SignedWebhookEnvelope:
    """Build an exact envelope for the authenticated demo organization."""

    normalized_now = _as_utc(now)
    if (
        settings.environment == "production"
        or settings.demo_mode is not True
        or not settings.webhook_enabled
        or settings.webhook_master_secret is None
    ):
        raise WebhookSimulatorUnavailable

    target = _active_target(
        db,
        organization_id=organization_id,
        now=normalized_now,
    )
    if target is None:
        raise WebhookSimulatorUnavailable
    integration_id, key_version = target

    current_timestamp = int(normalized_now.timestamp())
    timestamp = current_timestamp if scenario == "fresh" else current_timestamp - 301
    nonce = uuid4().hex
    event_id = f"evt_{nonce}"
    order_id = f"syn_order_{nonce}"
    order_number = f"DEMO-{int(nonce[:8], 16) % 10_000_000:07d}"
    occurred_at = (
        datetime.fromtimestamp(current_timestamp, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    raw_body = json.dumps(
        {
            "type": "payment.failed",
            "occurred_at": occurred_at,
            "data": {
                "order": {
                    "id": order_id,
                    "number": order_number,
                    "amount_minor": 12_900,
                    "currency": "USD",
                }
            },
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    integration_key = derive_webhook_integration_key(
        settings.webhook_master_secret.get_secret_value().encode("utf-8"),
        integration_id=integration_id,
        key_version=key_version,
    )
    signature = sign_webhook_request(
        integration_key=integration_key,
        timestamp=timestamp,
        integration_id=integration_id,
        event_id=event_id,
        raw_body=raw_body,
    )
    return SignedWebhookEnvelope(
        path=f"/api/webhooks/synthetic/{integration_id}",
        body=raw_body.decode("ascii"),
        timestamp=str(timestamp),
        event_id=event_id,
        signature=signature,
    )
