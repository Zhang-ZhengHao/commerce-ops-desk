"""End-to-end evidence for webhook bytes and unit-of-work boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from itertools import count
from typing import Any, cast
from uuid import uuid4

import pytest
from conftest import AppHarness
from fastapi import FastAPI
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.types import Message, Scope

from app.api import webhooks as webhook_api
from app.auth.webhook import (
    derive_webhook_integration_key,
    sign_webhook_request,
    verify_webhook_signature,
)
from app.models import Organization, WebhookIntegration
from app.services.webhook_processing import (
    WebhookProcessingResult,
    process_payment_failed_webhook,
)

WEBHOOK_MASTER_SECRET = "webhook-pipeline-test-master-secret-independent"
STREAMED_BODY = (
    b'{\n  "type": "payment.failed",\n  "occurred_at": "2026-10-07T11:59:00Z",\n'
    b'  "data": {"order": {"id": "syn_order_PIPEA1B2C3D4", '
    b'"number": "DEMO-6045", "amount_minor": 12900, "currency": "USD"}}\n}'
)


def _seed_active_target(harness: AppHarness) -> tuple[str, int]:
    integration_id = str(uuid4())
    key_version = 4
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory.begin() as database:
        organization = Organization(
            id=str(uuid4()),
            name="Webhook pipeline test workspace",
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


def _streamed_chunks() -> tuple[bytes, ...]:
    boundaries = (1, 31, 97, len(STREAMED_BODY))
    start = 0
    chunks: list[bytes] = []
    for end in boundaries:
        chunks.append(STREAMED_BODY[start:end])
        start = end
    return tuple(chunks)


def _post_streamed(
    app: FastAPI,
    *,
    path: str,
    headers: dict[str, str],
    chunks: tuple[bytes, ...],
) -> tuple[int, dict[str, object]]:
    request_messages: list[Message] = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    response_messages: list[Message] = []

    async def call_app() -> None:
        next_message = 0

        async def receive() -> Message:
            nonlocal next_message
            if next_message < len(request_messages):
                message = request_messages[next_message]
                next_message += 1
                return message
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            response_messages.append(message)

        raw_path = path.encode("ascii")
        raw_headers = [(b"host", b"commerceops.test")]
        raw_headers.extend(
            (name.lower().encode("ascii"), value.encode("ascii")) for name, value in headers.items()
        )
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": raw_path,
            "root_path": "",
            "query_string": b"",
            "headers": raw_headers,
            "client": ("198.51.100.101", 50_000),
            "server": ("commerceops.test", 80),
            "extensions": {},
            "state": {},
        }
        await app(scope, receive, send)

    asyncio.run(call_app())
    response_start = next(
        message for message in response_messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )
    return response_start["status"], cast(dict[str, object], json.loads(response_body))


def test_streamed_body_is_read_once_and_the_same_bytes_reach_auth_and_processing(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    event_id = "evt_PIPEBYTES01"

    real_request_body = Request.body
    request_body_calls = 0
    verified_bodies: list[bytes] = []
    processed_arguments: list[dict[str, Any]] = []
    real_verify = verify_webhook_signature
    real_process = process_payment_failed_webhook

    async def track_request_body(request: Request) -> bytes:
        nonlocal request_body_calls
        request_body_calls += 1
        return await real_request_body(request)

    def track_verification(**arguments: Any) -> bool:
        verified_bodies.append(cast(bytes, arguments["raw_body"]))
        return real_verify(**arguments)

    def track_processing(*args: Any, **arguments: Any) -> WebhookProcessingResult:
        processed_arguments.append(arguments.copy())
        return real_process(*args, **arguments)

    monkeypatch.setattr(Request, "body", track_request_body)
    monkeypatch.setattr(webhook_api, "verify_webhook_signature", track_verification)
    monkeypatch.setattr(webhook_api, "process_payment_failed_webhook", track_processing)

    response_status, response_body = _post_streamed(
        harness.app,
        path=f"/api/webhooks/synthetic/{integration_id}",
        headers=_signed_headers(
            integration_id=integration_id,
            key_version=key_version,
            timestamp=timestamp,
            event_id=event_id,
            raw_body=STREAMED_BODY,
        ),
        chunks=_streamed_chunks(),
    )

    assert response_status == 201
    assert response_body == {
        "status": "processed",
        "event_id": event_id,
        "case_id": response_body["case_id"],
        "replayed": False,
    }
    assert request_body_calls == 1
    assert verified_bodies == [STREAMED_BODY]
    assert len(processed_arguments) == 1
    assert processed_arguments[0]["payload_digest"] == sha256(STREAMED_BODY).hexdigest()


def test_successful_request_uses_three_ordered_closed_units_of_work(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    integration_id, key_version = _seed_active_target(harness)
    timestamp = int(harness.clock().timestamp())
    event_id = "evt_PIPESESS01"

    lifecycle: list[tuple[str, int]] = []
    created_sessions: list[TrackingSession] = []
    session_ids = count(1)

    class TrackingSession(Session):
        tracking_id: int
        close_observed: bool

        def __init__(self, **arguments: Any) -> None:
            super().__init__(**arguments)
            self.tracking_id = next(session_ids)
            self.close_observed = False
            created_sessions.append(self)
            lifecycle.append(("created", self.tracking_id))

        def close(self) -> None:
            super().close()
            self.close_observed = True
            lifecycle.append(("closed", self.tracking_id))

    def track_after_commit(database: Session) -> None:
        tracked_database = cast(TrackingSession, database)
        lifecycle.append(("after_commit", tracked_database.tracking_id))

    event.listen(TrackingSession, "after_commit", track_after_commit)
    engine = cast(Engine, harness.app.state.database_engine)
    tracking_factory = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        autoflush=False,
        expire_on_commit=False,
    )
    harness.app.state.session_factory = tracking_factory

    with harness.client() as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=STREAMED_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=event_id,
                raw_body=STREAMED_BODY,
            ),
        )

    assert response.status_code == 201
    assert len(created_sessions) == 3
    assert len({id(database) for database in created_sessions}) == 3
    assert lifecycle == [
        ("created", 1),
        ("after_commit", 1),
        ("closed", 1),
        ("created", 2),
        ("closed", 2),
        ("created", 3),
        ("after_commit", 3),
        ("closed", 3),
    ]
    assert all(database.close_observed for database in created_sessions)
