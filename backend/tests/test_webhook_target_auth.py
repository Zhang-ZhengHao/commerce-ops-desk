"""HTTP authentication boundaries for the public synthetic webhook ingress."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from conftest import AppHarness
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

from app.auth.webhook import (
    derive_webhook_integration_key,
    sign_webhook_request,
    verify_webhook_signature,
)
from app.models import Organization, WebhookIntegration

WEBHOOK_MASTER_SECRET = "webhook-target-auth-secret-that-is-independent"
AUTHENTICATION_FAILED = {
    "detail": {
        "code": "webhook_authentication_failed",
        "message": "Webhook authentication failed.",
    }
}
VALID_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T11:59:00Z",'
    b'"data":{"order":{"id":"syn_order_AUTHA1B2C3D4","number":"DEMO-1045",'
    b'"amount_minor":12900,"currency":"USD"}}}'
)


def _enabled_harness(
    app_harness_factory: Callable[..., AppHarness],
) -> AppHarness:
    return app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
        webhook_source_minute_limit=200,
    )


def _seed_target(
    harness: AppHarness,
    *,
    enabled: bool = True,
    is_demo: bool = True,
    expired: bool = False,
    key_version: int = 3,
) -> tuple[str, int]:
    integration_id = str(uuid4())
    now = harness.clock()
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        organization = Organization(
            id=str(uuid4()),
            name="Webhook target authentication workspace",
            is_demo=is_demo,
            case_note_count=0,
            webhook_event_count=0,
            created_at=now - timedelta(hours=1),
            expires_at=(now if expired else now + timedelta(hours=4)),
        )
        database.add(organization)
        database.flush()
        database.add(
            WebhookIntegration(
                id=integration_id,
                organization_id=organization.id,
                provider="synthetic",
                key_version=key_version,
                enabled=enabled,
                created_at=now,
                updated_at=now,
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


def _replace_header(
    headers: Iterable[tuple[str, str]],
    *,
    name: str,
    value: str,
) -> list[tuple[str, str]]:
    return [
        (header_name, value if header_name.lower() == name.lower() else header_value)
        for header_name, header_value in headers
    ]


def _auth_fingerprint(
    response: Response,
) -> tuple[int, bytes, tuple[tuple[str, str], ...]]:
    return (
        response.status_code,
        response.content,
        tuple(sorted(response.headers.multi_items())),
    )


def _assert_authentication_failed(response: Response) -> None:
    assert response.status_code == 401
    assert response.json() == AUTHENTICATION_FAILED


def test_raw_path_requires_canonical_ascii_uuid_and_conceals_all_failures(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = _enabled_harness(app_harness_factory)
    integration_id, key_version = _seed_target(harness)
    timestamp = int(harness.clock().timestamp())

    with harness.client() as client:
        accepted = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_PATHVALID01",
                raw_body=VALID_BODY,
            ),
        )

        unknown_id = str(uuid4())
        canonical_unknown = client.post(
            f"/api/webhooks/synthetic/{unknown_id}",
            content=VALID_BODY,
            headers=_signed_headers(
                integration_id=unknown_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id="evt_PATHUNKNOWN1",
                raw_body=VALID_BODY,
            ),
        )
        noncanonical_paths = {
            "percent-encoded": integration_id.replace("-", "%2D", 1),
            "uppercase": integration_id.upper(),
            "without-hyphens": integration_id.replace("-", ""),
            "trailing-slash": f"{integration_id}/",
            "encoded-slash": f"{integration_id}%2F",
        }
        noncanonical_responses = [
            client.post(
                f"/api/webhooks/synthetic/{path_id}",
                content=VALID_BODY,
                headers=_signed_headers(
                    integration_id=integration_id,
                    key_version=key_version,
                    timestamp=timestamp,
                    event_id=f"evt_PATHINVALID{index}",
                    raw_body=VALID_BODY,
                ),
            )
            for index, path_id in enumerate(noncanonical_paths.values(), start=1)
        ]

    assert accepted.status_code == 201
    failures = [canonical_unknown, *noncanonical_responses]
    for response in failures:
        _assert_authentication_failed(response)
    assert {_auth_fingerprint(response) for response in failures} == {
        _auth_fingerprint(canonical_unknown)
    }


def test_missing_duplicate_and_invalid_security_headers_share_one_401(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = _enabled_harness(app_harness_factory)
    integration_id, key_version = _seed_target(harness)
    timestamp = int(harness.clock().timestamp())
    base_headers = _signed_headers(
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id="evt_HEADERAUTH01",
        raw_body=VALID_BODY,
    )
    security_headers: Mapping[str, str] = {
        "X-Webhook-Timestamp": "01783504800",
        "X-Webhook-Event-Id": "evt_not-canonical",
        "X-Webhook-Signature": f"v1={'A' * 64}",
    }
    cases: list[tuple[str, list[tuple[str, str]]]] = []
    raw_base_headers = list(base_headers.items())
    for header_name, invalid_value in security_headers.items():
        cases.extend(
            [
                (
                    f"missing {header_name}",
                    [
                        (name, value)
                        for name, value in raw_base_headers
                        if name.lower() != header_name.lower()
                    ],
                ),
                (
                    f"duplicate {header_name}",
                    [
                        *raw_base_headers,
                        (header_name, base_headers[header_name]),
                    ],
                ),
                (
                    f"invalid {header_name}",
                    _replace_header(
                        raw_base_headers,
                        name=header_name,
                        value=invalid_value,
                    ),
                ),
            ]
        )

    responses: list[tuple[str, Response]] = []
    with harness.client() as client:
        for label, headers in cases:
            responses.append(
                (
                    label,
                    client.post(
                        f"/api/webhooks/synthetic/{integration_id}",
                        content=VALID_BODY,
                        headers=headers,
                    ),
                )
            )

    for label, response in responses:
        assert response.status_code == 401, label
        assert response.json() == AUTHENTICATION_FAILED, label
    assert len({_auth_fingerprint(response) for _, response in responses}) == 1


def test_invalid_targets_and_bad_signature_are_response_indistinguishable(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = _enabled_harness(app_harness_factory)
    timestamp = int(harness.clock().timestamp())
    unknown = (str(uuid4()), 7)
    disabled = _seed_target(harness, enabled=False)
    non_demo = _seed_target(harness, is_demo=False)
    expired = _seed_target(harness, expired=True)
    active = _seed_target(harness)
    targets = [unknown, disabled, non_demo, expired, active]

    responses: list[Response] = []
    with harness.client() as client:
        for index, (integration_id, key_version) in enumerate(targets, start=1):
            headers = _signed_headers(
                integration_id=integration_id,
                key_version=key_version,
                timestamp=timestamp,
                event_id=f"evt_TARGETAUTH{index}",
                raw_body=VALID_BODY,
            )
            if (integration_id, key_version) == active:
                headers["X-Webhook-Signature"] = f"v1={'0' * 64}"
            responses.append(
                client.post(
                    f"/api/webhooks/synthetic/{integration_id}",
                    content=VALID_BODY,
                    headers=headers,
                )
            )

    for response in responses:
        _assert_authentication_failed(response)
    assert len({_auth_fingerprint(response) for response in responses}) == 1


def test_every_invalid_target_executes_one_hmac_with_the_same_dummy_key(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _enabled_harness(app_harness_factory)
    timestamp = int(harness.clock().timestamp())
    targets = [
        (str(uuid4()), 7),
        _seed_target(harness, enabled=False),
        _seed_target(harness, is_demo=False),
        _seed_target(harness, expired=True),
    ]
    calls: list[dict[str, Any]] = []
    real_verify = verify_webhook_signature

    def record_and_verify(**kwargs: Any) -> bool:
        calls.append(dict(kwargs))
        return real_verify(**kwargs)

    monkeypatch.setattr(
        "app.api.webhooks.verify_webhook_signature",
        record_and_verify,
    )

    with harness.client() as client:
        for index, (integration_id, key_version) in enumerate(targets, start=1):
            call_count = len(calls)
            response = client.post(
                f"/api/webhooks/synthetic/{integration_id}",
                content=VALID_BODY,
                headers=_signed_headers(
                    integration_id=integration_id,
                    key_version=key_version,
                    timestamp=timestamp,
                    event_id=f"evt_DUMMYHMAC{index}",
                    raw_body=VALID_BODY,
                ),
            )

            _assert_authentication_failed(response)
            assert len(calls) == call_count + 1

    assert len(calls) == len(targets)
    assert len({call["integration_key"] for call in calls}) == 1


@pytest.mark.parametrize(
    ("raw_body", "content_type"),
    [
        (VALID_BODY, "text/plain"),
        (b"not-json", "application/json"),
    ],
    ids=["invalid-media", "invalid-json"],
)
def test_bad_signature_wins_before_media_or_json_validation(
    app_harness_factory: Callable[..., AppHarness],
    raw_body: bytes,
    content_type: str,
) -> None:
    harness = _enabled_harness(app_harness_factory)
    integration_id, key_version = _seed_target(harness)
    timestamp = int(harness.clock().timestamp())
    headers = _signed_headers(
        integration_id=integration_id,
        key_version=key_version,
        timestamp=timestamp,
        event_id="evt_AUTHPRECED01",
        raw_body=raw_body,
        content_type=content_type,
    )
    headers["X-Webhook-Signature"] = f"v1={'0' * 64}"

    with harness.client(raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/webhooks/synthetic/{integration_id}",
            content=raw_body,
            headers=headers,
        )

    _assert_authentication_failed(response)


def test_timestamp_window_accepts_both_edges_and_rejects_one_second_beyond(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = _enabled_harness(app_harness_factory)
    integration_id, key_version = _seed_target(harness)
    now_timestamp = int(harness.clock().timestamp())
    cases = [
        (-300, True),
        (300, True),
        (-301, False),
        (301, False),
    ]

    responses: list[tuple[int, bool, Response]] = []
    with harness.client() as client:
        for index, (offset, accepted) in enumerate(cases, start=1):
            marker = f"TIMEBOUND{index:02d}"
            raw_body = VALID_BODY.replace(b"AUTHA1B2C3D4", marker.encode("ascii"))
            timestamp = now_timestamp + offset
            responses.append(
                (
                    offset,
                    accepted,
                    client.post(
                        f"/api/webhooks/synthetic/{integration_id}",
                        content=raw_body,
                        headers=_signed_headers(
                            integration_id=integration_id,
                            key_version=key_version,
                            timestamp=timestamp,
                            event_id=f"evt_TIMEBOUND{index}",
                            raw_body=raw_body,
                        ),
                    ),
                )
            )

    for offset, accepted, response in responses:
        if accepted:
            assert response.status_code == 201, offset
            assert response.json()["replayed"] is False
        else:
            _assert_authentication_failed(response)
