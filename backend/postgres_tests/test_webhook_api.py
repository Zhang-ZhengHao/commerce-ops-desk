"""PostgreSQL HTTP-boundary proofs for the synthetic webhook ingress."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import webhooks as webhook_api
from app.auth.webhook import derive_webhook_integration_key, sign_webhook_request
from app.models import (
    AuditEvent,
    ExceptionCase,
    Order,
    Organization,
    WebhookEvent,
    WebhookIntegration,
)
from app.services.webhook_processing import (
    WebhookProcessingResult,
    process_payment_failed_webhook,
)

if TYPE_CHECKING:
    from postgres_tests.conftest import PostgresAppHarness

WEBHOOK_MASTER_SECRET = "postgres-route-master-secret-independent-of-session-key"
VALID_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T11:59:00Z",'
    b'"data":{"order":{"id":"syn_order_PGHTTPA1B2C3D4","number":"DEMO-9101",'
    b'"amount_minor":18750,"currency":"USD"}}}'
)
SERVICE_UNAVAILABLE = {
    "detail": {
        "code": "webhook_service_unavailable",
        "message": "Webhook service is temporarily unavailable.",
    }
}


def _webhook_harness(
    factory: Callable[..., PostgresAppHarness],
) -> PostgresAppHarness:
    harness = factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
        webhook_source_minute_limit=20,
        demo_webhook_event_limit=20,
    )
    assert harness.engine.dialect.name == "postgresql"
    return harness


def _seed_active_demo_target(harness: PostgresAppHarness) -> tuple[str, str, int]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    key_version = 4
    with Session(harness.engine) as database:
        database.add(
            Organization(
                id=organization_id,
                name="PostgreSQL webhook route workspace",
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
        database.commit()
    return organization_id, integration_id, key_version


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


def _assert_single_business_effect(
    harness: PostgresAppHarness,
    *,
    organization_id: str,
    integration_id: str,
    event_id: str,
    expected_case_id: str,
) -> None:
    with Session(harness.engine) as database:
        organization = database.get(Organization, organization_id)
        event_count = database.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(WebhookEvent.organization_id == organization_id)
        )
        order_count = database.scalar(
            select(func.count()).select_from(Order).where(Order.organization_id == organization_id)
        )
        case_count = database.scalar(
            select(func.count())
            .select_from(ExceptionCase)
            .where(ExceptionCase.organization_id == organization_id)
        )
        audit_count = database.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
        )
        inbox_event = database.scalar(
            select(WebhookEvent).where(WebhookEvent.organization_id == organization_id)
        )
        case = database.get(ExceptionCase, expected_case_id)

    assert organization is not None
    assert organization.webhook_event_count == 1
    assert (event_count, order_count, case_count, audit_count) == (1, 1, 1, 1)
    assert inbox_event is not None
    assert inbox_event.integration_id == integration_id
    assert inbox_event.external_event_id == event_id
    assert inbox_event.payload_digest == hashlib.sha256(VALID_BODY).hexdigest()
    assert inbox_event.case_id == expected_case_id
    assert inbox_event.processed_at is not None
    assert case is not None
    assert case.source_event_id == inbox_event.id


def test_concurrent_exact_http_deliveries_create_one_effect_and_one_replay(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _webhook_harness(postgres_app_harness_factory)
    organization_id, integration_id, key_version = _seed_active_demo_target(harness)
    event_id = "evt_PGHTTPRACE01"
    timestamp = int(harness.clock().timestamp())
    headers = _signed_headers(
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=VALID_BODY,
    )
    business_boundary = Barrier(2)
    observed_sessions: set[int] = set()
    observed_backend_pids: set[int] = set()
    observations_lock = Lock()
    real_processor = process_payment_failed_webhook

    def synchronized_processor(
        database: Session,
        **kwargs: Any,
    ) -> WebhookProcessingResult:
        backend_pid = database.scalar(text("SELECT pg_backend_pid()"))
        assert backend_pid is not None
        with observations_lock:
            observed_sessions.add(id(database))
            observed_backend_pids.add(int(backend_pid))
        business_boundary.wait(timeout=10)
        return real_processor(database, **kwargs)

    monkeypatch.setattr(
        webhook_api,
        "process_payment_failed_webhook",
        synchronized_processor,
    )

    def deliver(index: int) -> tuple[int, str, bool]:
        with harness.client(
            source_ip=f"198.51.100.{121 + index}",
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                f"/api/webhooks/synthetic/{integration_id}",
                content=VALID_BODY,
                headers=headers,
            )
        payload: dict[str, object] = response.json()
        assert payload["status"] == "processed"
        assert payload["event_id"] == event_id
        case_id = payload["case_id"]
        replayed = payload["replayed"]
        assert isinstance(case_id, str)
        assert isinstance(replayed, bool)
        return response.status_code, case_id, replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, index) for index in range(2)]
        outcomes = [future.result(timeout=20) for future in futures]

    assert {status_code for status_code, _case_id, _replayed in outcomes} == {200, 201}
    assert {case_id for _status_code, case_id, _replayed in outcomes} == {outcomes[0][1]}
    assert {replayed for _status_code, _case_id, replayed in outcomes} == {False, True}
    assert len(observed_sessions) == 2
    assert len(observed_backend_pids) == 2
    _assert_single_business_effect(
        harness,
        organization_id=organization_id,
        integration_id=integration_id,
        event_id=event_id,
        expected_case_id=outcomes[0][1],
    )


def test_unknown_commit_outcome_returns_503_then_retries_as_exact_replay(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _webhook_harness(postgres_app_harness_factory)
    organization_id, integration_id, key_version = _seed_active_demo_target(harness)
    event_id = "evt_PGHTTPUNKNOWN1"
    real_processor = process_payment_failed_webhook
    invocation_count = 0

    def lose_first_commit_acknowledgement(
        database: Session,
        **kwargs: Any,
    ) -> WebhookProcessingResult:
        nonlocal invocation_count
        result = real_processor(database, **kwargs)
        invocation_count += 1
        if invocation_count == 1:
            raise OperationalError(
                "simulated unknown commit outcome",
                {},
                OSError("commit acknowledgement lost"),
            )
        return result

    monkeypatch.setattr(
        webhook_api,
        "process_payment_failed_webhook",
        lose_first_commit_acknowledgement,
    )

    first_timestamp = int(harness.clock().timestamp())
    with harness.client(
        source_ip="198.51.100.131",
        raise_server_exceptions=False,
    ) as first_client:
        first = first_client.post(
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

    assert first.status_code == 503
    assert first.json() == SERVICE_UNAVAILABLE
    assert first.headers["Retry-After"] == "1"

    harness.clock.advance(seconds=1)
    retry_timestamp = int(harness.clock().timestamp())
    assert retry_timestamp != first_timestamp
    with harness.client(
        source_ip="198.51.100.132",
        raise_server_exceptions=False,
    ) as retry_client:
        retry = retry_client.post(
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

    assert retry.status_code == 200
    assert retry.json() == {
        "status": "processed",
        "event_id": event_id,
        "case_id": retry.json()["case_id"],
        "replayed": True,
    }
    assert invocation_count == 2
    retry_case_id = retry.json()["case_id"]
    assert isinstance(retry_case_id, str)
    _assert_single_business_effect(
        harness,
        organization_id=organization_id,
        integration_id=integration_id,
        event_id=event_id,
        expected_case_id=retry_case_id,
    )
