"""Public HTTP contract for the synthetic webhook ingress."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import cast
from uuid import uuid4

from conftest import AppHarness
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.webhook import derive_webhook_integration_key, sign_webhook_request
from app.models import AuditEvent, ExceptionCase, Organization, WebhookEvent, WebhookIntegration

WEBHOOK_MASTER_SECRET = "webhook-test-master-secret-that-is-independent"
AUTHENTICATION_FAILED = {
    "detail": {
        "code": "webhook_authentication_failed",
        "message": "Webhook authentication failed.",
    }
}
VALID_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T11:59:00Z",'
    b'"data":{"order":{"id":"syn_order_HTTPA1B2C3D4","number":"DEMO-1045",'
    b'"amount_minor":12900,"currency":"USD"}}}'
)


def _seed_active_target(harness: AppHarness) -> tuple[str, int]:
    integration_id = str(uuid4())
    key_version = 3
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        organization = Organization(
            id=str(uuid4()),
            name="Webhook API test workspace",
            is_demo=True,
            case_note_count=0,
            webhook_event_count=0,
            created_at=harness.clock(),
            expires_at=harness.clock() + timedelta(hours=4),
        )
        database.add(organization)
        database.flush()
        database.add(
            WebhookIntegration(
                id=integration_id,
                organization_id=organization.id,
                provider="synthetic",
                key_version=key_version,
                enabled=True,
                created_at=harness.clock(),
                updated_at=harness.clock(),
            )
        )
        database.commit()
    return integration_id, key_version


def _signed_headers(
    *,
    integration_id: str,
    key_version: int,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
) -> dict[str, str]:
    integration_key = derive_webhook_integration_key(
        WEBHOOK_MASTER_SECRET.encode("utf-8"),
        integration_id=integration_id,
        key_version=key_version,
    )
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Event-Id": event_id,
        "X-Webhook-Signature": sign_webhook_request(
            integration_key=integration_key,
            timestamp=timestamp,
            integration_id=integration_id,
            event_id=event_id,
            raw_body=raw_body,
        ),
    }


def test_webhook_route_is_not_registered_when_ingress_is_disabled(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(webhook_enabled=False)

    with harness.client() as client:
        response = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")

    assert response.status_code == 404
    assert "/api/webhooks/synthetic/{integration_id}" not in harness.app.openapi()["paths"]


def test_enabled_webhook_route_hides_a_malformed_target_behind_fixed_401(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )

    with harness.client() as client:
        response = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")

    assert response.status_code == 401
    assert response.json() == AUTHENTICATION_FAILED
    assert "/api/webhooks/synthetic/{integration_id}" in harness.app.openapi()["paths"]


def test_valid_webhook_without_browser_cookie_or_csrf_creates_one_business_effect(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    integration_id, key_version = _seed_active_target(harness)
    event_id = "evt_HTTPA1B2C3D4"
    timestamp = int(harness.clock().timestamp())

    with harness.client() as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=event_id,
                raw_body=VALID_BODY,
            ),
        )

    assert response.status_code == 201
    assert response.json() == {
        "status": "processed",
        "event_id": event_id,
        "case_id": response.json()["case_id"],
        "replayed": False,
    }
    assert response.cookies == {}

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        assert database.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        assert database.scalar(select(func.count()).select_from(ExceptionCase)) == 1
        assert database.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_exact_webhook_replay_returns_the_committed_case_without_duplicate_effects(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    integration_id, key_version = _seed_active_target(harness)
    event_id = "evt_HTTPREPLAY01"
    timestamp = int(harness.clock().timestamp())
    original_headers = _signed_headers(
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=VALID_BODY,
    )

    with harness.client() as client:
        created = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=original_headers,
        )
        harness.clock.advance(seconds=301)
        stale = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=original_headers,
        )
        replayed = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=int(harness.clock().timestamp()),
                event_id=event_id,
                raw_body=VALID_BODY,
            ),
        )

    assert created.status_code == 201
    assert stale.status_code == 401
    assert stale.json() == AUTHENTICATION_FAILED
    assert replayed.status_code == 200
    assert created.json()["replayed"] is False
    assert replayed.json() == {
        **created.json(),
        "replayed": True,
    }

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        assert database.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        assert database.scalar(select(func.count()).select_from(ExceptionCase)) == 1
        assert database.scalar(select(func.count()).select_from(AuditEvent)) == 1
