"""Leak-safe logging and early-rejection proofs for public webhooks."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import timedelta
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from conftest import AppHarness
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.api import webhooks as webhook_api
from app.auth.webhook import (
    derive_webhook_integration_key,
    sign_webhook_request,
    verify_webhook_signature,
)
from app.models import Organization, WebhookIntegration

DEFAULT_MASTER_SECRET = "webhook-logging-test-master-secret-independent"
MASTER_SECRET_CANARY = "LEAK_CANARY_MASTER_SECRET_4f8a1c93d6e2"
BODY_ORDER_ID_CANARY = "syn_order_LEAKBODYCANARY7K9Q"
EXTRA_HEADER_CANARY = "LEAK_CANARY_EXTRA_HEADER_7d21b9"
SOURCE_IP_CANARY = "2001:db8:ca11:abba::7"
OPERATIONAL_ERROR_MESSAGE_CANARY = "LEAK_CANARY_OPERATIONAL_MESSAGE_9482"
OPERATIONAL_ERROR_PARAMS_CANARY = "LEAK_CANARY_OPERATIONAL_PARAMS_a61f"
OPERATIONAL_ERROR_ORIGIN_CANARY = "LEAK_CANARY_OPERATIONAL_ORIGIN_c37e"

AUTHENTICATION_FAILED = {
    "detail": {
        "code": "webhook_authentication_failed",
        "message": "Webhook authentication failed.",
    }
}
SOURCE_RATE_LIMITED = {
    "detail": {
        "code": "webhook_ingress_rate_limited",
        "message": "Webhook ingress rate limit exceeded.",
    }
}
SERVICE_UNAVAILABLE = {
    "detail": {
        "code": "webhook_service_unavailable",
        "message": "Webhook service is temporarily unavailable.",
    }
}


def _valid_body(order_id: str) -> bytes:
    return (
        '{"type":"payment.failed","occurred_at":"2026-10-07T11:59:00Z",'
        f'"data":{{"order":{{"id":"{order_id}","number":"DEMO-8107",'
        '"amount_minor":12900,"currency":"USD"}}}'
    ).encode("ascii")


def _seed_active_target(harness: AppHarness) -> tuple[str, int]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    key_version = 4
    now = harness.clock()
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory.begin() as database:
        database.add(
            Organization(
                id=organization_id,
                name="Webhook logging test workspace",
                is_demo=True,
                case_note_count=0,
                webhook_event_count=0,
                created_at=now,
                expires_at=now + timedelta(hours=4),
            )
        )
        database.add(
            WebhookIntegration(
                id=integration_id,
                organization_id=organization_id,
                provider="synthetic",
                key_version=key_version,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
    return integration_id, key_version


def _signed_headers(
    *,
    master_secret: str,
    integration_id: str,
    key_version: int,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
    content_type: str = "application/json",
) -> dict[str, str]:
    integration_key = derive_webhook_integration_key(
        master_secret.encode("utf-8"),
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


def _application_error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.startswith("app.") and record.levelno >= logging.ERROR
    ]


def test_processing_failure_never_reflects_or_logs_any_request_or_database_canary(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=MASTER_SECRET_CANARY,
        webhook_source_minute_limit=200,
    )
    integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    raw_body = _valid_body(BODY_ORDER_ID_CANARY)
    headers = _signed_headers(
        master_secret=MASTER_SECRET_CANARY,
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id="evt_LOGCANARY01",
        raw_body=raw_body,
    )
    signature_canary = headers["X-Webhook-Signature"]
    headers["X-Request-ID"] = EXTRA_HEADER_CANARY

    def fail_processing(*_args: object, **_kwargs: object) -> None:
        raise OperationalError(
            OPERATIONAL_ERROR_MESSAGE_CANARY,
            {"bound_value": OPERATIONAL_ERROR_PARAMS_CANARY},
            RuntimeError(OPERATIONAL_ERROR_ORIGIN_CANARY),
        )

    monkeypatch.setattr(webhook_api, "process_payment_failed_webhook", fail_processing)
    caplog.set_level(logging.ERROR)
    caplog.clear()
    capsys.readouterr()

    with harness.client(source_ip=SOURCE_IP_CANARY, raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=raw_body,
            headers=headers,
        )

    captured = capsys.readouterr()
    assert response.status_code == 503
    assert response.json() == SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "1"

    records = _application_error_records(caplog)
    assert len(records) == 1
    assert caplog.records == records
    record = records[0]
    message_match = re.fullmatch(
        r"request_id=([0-9a-f]{32}) phase=processing "
        r"code=webhook_service_unavailable exception=OperationalError",
        record.getMessage(),
    )
    assert message_match is not None
    assert record.name == "app.api.webhooks"
    assert record.msg == ("request_id=%s phase=%s code=webhook_service_unavailable exception=%s")
    assert record.args == (message_match.group(1), "processing", "OperationalError")
    assert record.exc_info is None
    assert record.stack_info is None

    sinks = {
        "response": response.text,
        "caplog": caplog.text,
        "stdout": captured.out,
        "stderr": captured.err,
    }
    canaries = (
        MASTER_SECRET_CANARY,
        signature_canary,
        signature_canary.removeprefix("v1="),
        raw_body.decode("ascii"),
        BODY_ORDER_ID_CANARY,
        EXTRA_HEADER_CANARY,
        SOURCE_IP_CANARY,
        OPERATIONAL_ERROR_MESSAGE_CANARY,
        OPERATIONAL_ERROR_PARAMS_CANARY,
        OPERATIONAL_ERROR_ORIGIN_CANARY,
    )
    for sink_name, sink in sinks.items():
        for canary in canaries:
            assert canary not in sink, f"{sink_name} leaked {canary}"


def test_expected_auth_media_payload_and_conflict_responses_emit_no_application_error_log(
    app_harness_factory: Callable[..., AppHarness],
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=DEFAULT_MASTER_SECRET,
        webhook_source_minute_limit=200,
    )
    integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    valid_body = _valid_body("syn_order_LOGMATRIXA19")
    invalid_body = b'{"type":"payment.failed","occurred_at":'
    conflicting_body = valid_body.replace(b'{"type"', b'{ "type"', 1)
    caplog.set_level(logging.ERROR)
    caplog.clear()

    with harness.client(raise_server_exceptions=False) as client:
        unauthorized = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")
        unsupported = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=valid_body,
            headers=_signed_headers(
                master_secret=DEFAULT_MASTER_SECRET,
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_LOGMEDIA001",
                raw_body=valid_body,
                content_type="text/plain",
            ),
        )
        invalid = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=invalid_body,
            headers=_signed_headers(
                master_secret=DEFAULT_MASTER_SECRET,
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_LOGPAYLOAD01",
                raw_body=invalid_body,
            ),
        )
        created = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=valid_body,
            headers=_signed_headers(
                master_secret=DEFAULT_MASTER_SECRET,
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_LOGCONFLICT1",
                raw_body=valid_body,
            ),
        )
        conflict = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=conflicting_body,
            headers=_signed_headers(
                master_secret=DEFAULT_MASTER_SECRET,
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_LOGCONFLICT1",
                raw_body=conflicting_body,
            ),
        )

    assert unauthorized.status_code == 401
    assert unauthorized.json() == AUTHENTICATION_FAILED
    assert unsupported.status_code == 415
    assert unsupported.json()["detail"]["code"] == "webhook_media_type_unsupported"
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "webhook_payload_invalid"
    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "webhook_event_conflict"
    assert _application_error_records(caplog) == []


def test_exhausted_source_limit_short_circuits_every_later_webhook_stage(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=DEFAULT_MASTER_SECRET,
        webhook_source_minute_limit=1,
    )
    integration_id, key_version = _seed_active_target(harness)
    raw_body = _valid_body("syn_order_SHORTCIRCUIT9")
    timestamp = int(harness.clock().timestamp())
    headers = _signed_headers(
        master_secret=DEFAULT_MASTER_SECRET,
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id="evt_SHORTCIRCUIT1",
        raw_body=raw_body,
    )
    source_ip = "198.51.100.246"

    with harness.client(source_ip=source_ip, raise_server_exceptions=False) as client:
        admitted = client.post("/api/webhooks/synthetic/not-a-uuid", content=b"{}")
        assert admitted.status_code == 401

        target_lookup = Mock(name="find_active_demo_webhook_target")
        signature_verify = Mock(name="verify_webhook_signature")
        payload_parse = Mock(name="parse_synthetic_webhook_event")
        business_process = Mock(name="process_payment_failed_webhook")
        monkeypatch.setattr(webhook_api, "find_active_demo_webhook_target", target_lookup)
        monkeypatch.setattr(webhook_api, "verify_webhook_signature", signature_verify)
        monkeypatch.setattr(webhook_api, "parse_synthetic_webhook_event", payload_parse)
        monkeypatch.setattr(webhook_api, "process_payment_failed_webhook", business_process)

        limited = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=raw_body,
            headers=headers,
        )

    assert limited.status_code == 429
    assert limited.json() == SOURCE_RATE_LIMITED
    assert limited.headers["Retry-After"] == "60"
    target_lookup.assert_not_called()
    signature_verify.assert_not_called()
    payload_parse.assert_not_called()
    business_process.assert_not_called()


def test_malformed_path_and_headers_each_still_execute_exactly_one_hmac(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=DEFAULT_MASTER_SECRET,
        webhook_source_minute_limit=200,
    )
    integration_id, key_version = _seed_active_target(harness)
    raw_body = _valid_body("syn_order_MALFORMEDHMAC9")
    timestamp = int(harness.clock().timestamp())
    valid_headers = _signed_headers(
        master_secret=DEFAULT_MASTER_SECRET,
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id="evt_MALFORMED01",
        raw_body=raw_body,
    )
    verifier = Mock(wraps=verify_webhook_signature)
    monkeypatch.setattr(webhook_api, "verify_webhook_signature", verifier)

    with harness.client(raise_server_exceptions=False) as client:
        malformed_path = client.post(
            "/api/webhooks/synthetic/not-a-uuid",
            content=raw_body,
            headers=valid_headers,
        )
        assert malformed_path.status_code == 401
        assert malformed_path.json() == AUTHENTICATION_FAILED
        verifier.assert_called_once()

        verifier.reset_mock()
        malformed_headers = dict(valid_headers)
        malformed_headers["X-Webhook-Signature"] = "v1=not-lowercase-hex"
        malformed_header = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=raw_body,
            headers=malformed_headers,
        )

    assert malformed_header.status_code == 401
    assert malformed_header.json() == AUTHENTICATION_FAILED
    verifier.assert_called_once()
