"""HTTP webhook errors preserve source accounting and business atomicity."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from conftest import AppHarness
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.api import webhooks as webhook_api
from app.auth.webhook import (
    derive_webhook_integration_key,
    sign_webhook_request,
    verify_webhook_signature,
)
from app.models import (
    AuditEvent,
    ExceptionCase,
    Order,
    Organization,
    RateLimit,
    WebhookEvent,
    WebhookIntegration,
)
from app.services.webhook_ingress import derive_webhook_source_digest
from app.services.webhook_processing import (
    process_payment_failed_webhook as real_process_payment_failed_webhook,
)

WEBHOOK_MASTER_SECRET = "webhook-transaction-test-master-secret-independent"
VALID_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T11:59:00Z",'
    b'"data":{"order":{"id":"syn_order_TXA1B2C3D4","number":"DEMO-2045",'
    b'"amount_minor":12900,"currency":"USD"}}}'
)
VALID_BODY_WITH_WHITESPACE = VALID_BODY.replace(b'{"type"', b'{ "type"', 1)
VALID_BODY_WITH_CHANGED_AMOUNT = VALID_BODY.replace(
    b'"amount_minor":12900',
    b'"amount_minor":13900',
)
INVALID_BODY = b'{"type":"payment.failed","occurred_at":'

AUTHENTICATION_FAILED = {
    "detail": {
        "code": "webhook_authentication_failed",
        "message": "Webhook authentication failed.",
    }
}
MEDIA_TYPE_UNSUPPORTED = {
    "detail": {
        "code": "webhook_media_type_unsupported",
        "message": "Webhook media type is unsupported.",
    }
}
PAYLOAD_INVALID = {
    "detail": {
        "code": "webhook_payload_invalid",
        "message": "Webhook payload is invalid.",
    }
}
EVENT_CONFLICT = {
    "detail": {
        "code": "webhook_event_conflict",
        "message": "Webhook event conflicts with stored data.",
    }
}
SOURCE_RATE_LIMITED = {
    "detail": {
        "code": "webhook_ingress_rate_limited",
        "message": "Webhook ingress rate limit exceeded.",
    }
}
BUSINESS_LIMIT_REACHED = {
    "detail": {
        "code": "demo_webhook_limit_reached",
        "message": "Demo webhook event limit reached.",
    }
}
SERVICE_UNAVAILABLE = {
    "detail": {
        "code": "webhook_service_unavailable",
        "message": "Webhook service is temporarily unavailable.",
    }
}


def _seed_active_target(harness: AppHarness) -> tuple[str, str, int]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    key_version = 2
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory.begin() as database:
        database.add(
            Organization(
                id=organization_id,
                name="Webhook transaction test workspace",
                is_demo=True,
                case_note_count=0,
                webhook_event_count=0,
                created_at=harness.clock(),
                expires_at=harness.clock() + timedelta(hours=4),
            )
        )
        database.add(
            WebhookIntegration(
                id=integration_id,
                organization_id=organization_id,
                provider="synthetic",
                key_version=key_version,
                enabled=True,
                created_at=harness.clock(),
                updated_at=harness.clock(),
            )
        )
    return organization_id, integration_id, key_version


def _signed_headers(
    *,
    integration_id: str,
    key_version: int,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
    content_type: str = "application/json",
) -> dict[str, str]:
    integration_key = derive_webhook_integration_key(
        WEBHOOK_MASTER_SECRET.encode("utf-8"),
        integration_id=integration_id,
        key_version=key_version,
    )
    return {
        "Content-Type": content_type,
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


def _persisted_source_count(harness: AppHarness, *, source_ip: str) -> int | None:
    digest = derive_webhook_source_digest(
        WEBHOOK_MASTER_SECRET.encode("utf-8"),
        normalized_source=source_ip,
    )
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        return database.scalar(select(RateLimit.count).where(RateLimit.source_digest == digest))


def _business_counts(harness: AppHarness) -> tuple[int, int, int]:
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        return (
            database.scalar(select(func.count()).select_from(WebhookEvent)) or 0,
            database.scalar(select(func.count()).select_from(ExceptionCase)) or 0,
            database.scalar(select(func.count()).select_from(AuditEvent)) or 0,
        )


def test_authentication_failure_keeps_its_committed_source_attempt(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    source_ip = "198.51.100.81"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        response = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")

    assert response.status_code == 401
    assert response.json() == AUTHENTICATION_FAILED
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (0, 0, 0)


@pytest.mark.parametrize(
    ("raw_body", "content_type", "expected_status", "expected_body"),
    [
        (VALID_BODY, "text/plain", 415, MEDIA_TYPE_UNSUPPORTED),
        (INVALID_BODY, "application/json", 422, PAYLOAD_INVALID),
    ],
)
def test_authenticated_media_and_payload_errors_keep_their_source_attempts(
    app_harness_factory: Callable[..., AppHarness],
    raw_body: bytes,
    content_type: str,
    expected_status: int,
    expected_body: dict[str, object],
) -> None:
    source_ip = f"198.51.100.{expected_status - 334}"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    _organization_id, integration_id, key_version = _seed_active_target(harness)
    event_id = f"evt_TXERROR{expected_status}"
    timestamp = int(harness.clock().timestamp())

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=raw_body,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=event_id,
                raw_body=raw_body,
                content_type=content_type,
            ),
        )

    assert response.status_code == expected_status
    assert response.json() == expected_body
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (0, 0, 0)


def test_changed_raw_bytes_for_the_same_event_return_conflict_without_partial_writes(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    source_ip = "198.51.100.84"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    organization_id, integration_id, key_version = _seed_active_target(harness)
    event_id = "evt_TXCONFLICT01"
    timestamp = int(harness.clock().timestamp())

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        created = client.post(
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
        conflicted = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY_WITH_WHITESPACE,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=event_id,
                raw_body=VALID_BODY_WITH_WHITESPACE,
            ),
        )

    assert created.status_code == 201
    assert conflicted.status_code == 409
    assert conflicted.json() == EVENT_CONFLICT
    assert _persisted_source_count(harness, source_ip=source_ip) == 2
    assert _business_counts(harness) == (1, 1, 1)

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 1
        )


def test_demo_event_allowance_returns_a_distinct_429_and_rolls_back_the_new_event(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    source_ip = "198.51.100.85"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
        demo_webhook_event_limit=1,
    )
    organization_id, integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        created = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_TXALLOW001",
                raw_body=VALID_BODY,
            ),
        )
        limited = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_TXALLOW002",
                raw_body=VALID_BODY,
            ),
        )

    assert created.status_code == 201
    assert limited.status_code == 429
    assert limited.json() == BUSINESS_LIMIT_REACHED
    assert _persisted_source_count(harness, source_ip=source_ip) == 2
    assert _business_counts(harness) == (1, 1, 1)

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 1
        )


def test_changed_order_snapshot_for_a_new_event_rolls_back_every_new_business_write(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    source_ip = "198.51.100.90"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    organization_id, integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    original_event_id = "evt_TXORDER001"
    conflicting_event_id = "evt_TXORDER002"

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        created = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=original_event_id,
                raw_body=VALID_BODY,
            ),
        )
        conflicted = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY_WITH_CHANGED_AMOUNT,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=conflicting_event_id,
                raw_body=VALID_BODY_WITH_CHANGED_AMOUNT,
            ),
        )

    assert created.status_code == 201
    assert conflicted.status_code == 409
    assert conflicted.json() == EVENT_CONFLICT
    assert _persisted_source_count(harness, source_ip=source_ip) == 2
    assert _business_counts(harness) == (1, 1, 1)

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        assert database.scalars(select(WebhookEvent.external_event_id)).all() == [original_event_id]
        order = database.scalar(select(Order))
        assert order is not None
        assert order.amount_minor == 12_900
        assert order.payment_status == "failed"
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 1
        )


def test_source_limit_returns_retry_after_and_uses_a_distinct_429_code(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    source_ip = "198.51.100.86"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
        webhook_source_minute_limit=1,
    )

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        admitted = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")
        limited = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")

    assert admitted.status_code == 401
    assert limited.status_code == 429
    assert limited.json() == SOURCE_RATE_LIMITED
    assert limited.json()["detail"]["code"] != BUSINESS_LIMIT_REACHED["detail"]["code"]
    assert limited.headers["Retry-After"] == "60"
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (0, 0, 0)


def test_database_failure_returns_retryable_503_without_business_writes(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ip = "198.51.100.87"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    _organization_id, integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())

    def fail_processing(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("database unavailable", {}, OSError("database unavailable"))

    monkeypatch.setattr(webhook_api, "process_payment_failed_webhook", fail_processing)

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_TXDBFAIL01",
                raw_body=VALID_BODY,
            ),
        )

    assert response.status_code == 503
    assert response.json() == SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "1"
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (0, 0, 0)


def test_unknown_commit_outcome_is_retryable_without_automatic_reprocessing(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ip = "198.51.100.89"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    organization_id, integration_id, key_version = _seed_active_target(harness)
    event_id = "evt_TXUNKNOWN01"
    first_timestamp = int(harness.clock().timestamp())
    processor_calls = 0

    def commit_then_report_unknown(*args: Any, **kwargs: Any) -> Any:
        nonlocal processor_calls
        processor_calls += 1
        result = real_process_payment_failed_webhook(*args, **kwargs)
        if processor_calls == 1:
            raise OperationalError("commit outcome unknown", {}, OSError("connection lost"))
        return result

    monkeypatch.setattr(
        webhook_api,
        "process_payment_failed_webhook",
        commit_then_report_unknown,
    )

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        unavailable = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=first_timestamp,
                event_id=event_id,
                raw_body=VALID_BODY,
            ),
        )

    assert unavailable.status_code == 503
    assert unavailable.json() == SERVICE_UNAVAILABLE
    assert unavailable.headers["Retry-After"] == "1"
    assert processor_calls == 1
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (1, 1, 1)

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        committed_case_id = database.scalar(select(ExceptionCase.id))
        assert committed_case_id is not None
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 1
        )

    harness.clock.advance(seconds=1)
    retry_timestamp = int(harness.clock().timestamp())
    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        replayed = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=retry_timestamp,
                event_id=event_id,
                raw_body=VALID_BODY,
            ),
        )

    assert replayed.status_code == 200
    assert replayed.json() == {
        "status": "processed",
        "event_id": event_id,
        "case_id": committed_case_id,
        "replayed": True,
    }
    assert processor_calls == 2
    assert _persisted_source_count(harness, source_ip=source_ip) == 2
    assert _business_counts(harness) == (1, 1, 1)
    with factory() as database:
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 1
        )


def test_key_rotation_after_authentication_invalidates_the_old_signature_before_writes(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ip = "198.51.100.88"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    organization_id, integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)

    def verify_then_rotate(**kwargs: Any) -> bool:
        accepted = verify_webhook_signature(**kwargs)
        assert accepted is True
        with factory.begin() as database:
            database.execute(
                update(WebhookIntegration)
                .where(WebhookIntegration.id == integration_id)
                .values(key_version=key_version + 1)
            )
        return True

    monkeypatch.setattr(webhook_api, "verify_webhook_signature", verify_then_rotate)

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_TXROTATION01",
                raw_body=VALID_BODY,
            ),
        )

    assert response.status_code == 401
    assert response.json() == AUTHENTICATION_FAILED
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (0, 0, 0)
    with factory() as database:
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 0
        )


def test_target_disabled_after_authentication_is_rejected_before_business_writes(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ip = "198.51.100.91"
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    organization_id, integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)

    def verify_then_disable(**kwargs: Any) -> bool:
        accepted = verify_webhook_signature(**kwargs)
        assert accepted is True
        with factory.begin() as database:
            database.execute(
                update(WebhookIntegration)
                .where(WebhookIntegration.id == integration_id)
                .values(enabled=False)
            )
        return True

    monkeypatch.setattr(webhook_api, "verify_webhook_signature", verify_then_disable)

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_TXDISABLED1",
                raw_body=VALID_BODY,
            ),
        )

    assert response.status_code == 401
    assert response.json() == AUTHENTICATION_FAILED
    assert _persisted_source_count(harness, source_ip=source_ip) == 1
    assert _business_counts(harness) == (0, 0, 0)
    with factory() as database:
        assert (
            database.scalar(
                select(Organization.webhook_event_count).where(Organization.id == organization_id)
            )
            == 0
        )
        assert (
            database.scalar(
                select(WebhookIntegration.enabled).where(WebhookIntegration.id == integration_id)
            )
            is False
        )
