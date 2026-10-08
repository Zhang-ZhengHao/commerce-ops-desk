"""Manager-only signed-envelope contracts for the synthetic provider demo."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.webhook import derive_webhook_integration_key, verify_webhook_signature
from app.models import Organization, WebhookEvent, WebhookIntegration

SIMULATOR_PATH = "/api/demo/webhooks/envelope"
MASTER_SECRET = "simulator-test-webhook-master-secret-32-bytes"
MASTER_SECRET_CANARY = "SIMULATOR_MASTER_SECRET_CANARY_7e912d4a"
VALIDATION_FAILED = {
    "detail": {
        "code": "request_validation_failed",
        "message": "Request validation failed.",
    }
}
BODY_TOO_LARGE = {
    "detail": {
        "code": "request_body_too_large",
        "message": "Request body exceeds the allowed size.",
    }
}
WEBHOOK_AUTHENTICATION_FAILED = {
    "detail": {
        "code": "webhook_authentication_failed",
        "message": "Webhook authentication failed.",
    }
}


def _enabled_harness(
    app_harness_factory: Callable[..., AppHarness],
    **overrides: object,
) -> AppHarness:
    settings: dict[str, object] = {
        "webhook_enabled": True,
        "webhook_master_secret": MASTER_SECRET,
    }
    settings.update(overrides)
    return app_harness_factory(**settings)


def _simulator_headers(csrf_token: str, *, origin: str = SAME_ORIGIN) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": origin,
        "X-CSRF-Token": csrf_token,
    }


def _post_envelope(
    client: TestClient,
    *,
    csrf_token: str,
    scenario: str,
) -> Any:
    return client.post(
        SIMULATOR_PATH,
        headers=_simulator_headers(csrf_token),
        json={"scenario": scenario},
    )


def _session_factory(harness: AppHarness) -> sessionmaker[Session]:
    return cast(sessionmaker[Session], harness.app.state.session_factory)


def _integration_for(
    harness: AppHarness,
    *,
    organization_id: str,
) -> tuple[str, int]:
    with _session_factory(harness)() as database:
        integration_id, key_version = database.execute(
            select(WebhookIntegration.id, WebhookIntegration.key_version).where(
                WebhookIntegration.organization_id == organization_id,
                WebhookIntegration.provider == "synthetic",
            )
        ).one()
    return integration_id, key_version


def _business_counts(
    harness: AppHarness,
    *,
    organization_id: str,
) -> tuple[int, int]:
    with _session_factory(harness)() as database:
        event_count = int(
            database.scalar(
                select(func.count())
                .select_from(WebhookEvent)
                .where(WebhookEvent.organization_id == organization_id)
            )
            or 0
        )
        allowance = database.scalar(
            select(Organization.webhook_event_count).where(Organization.id == organization_id)
        )
    assert allowance is not None
    return event_count, allowance


def _delivery_headers(envelope: dict[str, object]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": cast(str, envelope["timestamp"]),
        "X-Webhook-Event-Id": cast(str, envelope["event_id"]),
        "X-Webhook-Signature": cast(str, envelope["signature"]),
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"demo_mode": False},
        {"webhook_enabled": False, "webhook_master_secret": None},
        {"environment": "production", "demo_mode": True},
    ],
)
def test_simulator_route_is_absent_when_disabled_or_in_production_even_if_misconfigured(
    app_harness_factory: Callable[..., AppHarness],
    settings: dict[str, object],
) -> None:
    harness = _enabled_harness(app_harness_factory, **settings)

    with harness.client() as client:
        response = client.post(
            SIMULATOR_PATH,
            headers={"Origin": SAME_ORIGIN},
            json={"scenario": "fresh"},
        )
        schema = client.get("/openapi.json").json()

    assert response.status_code == 404
    assert SIMULATOR_PATH not in schema["paths"]


def test_auth_role_origin_csrf_and_body_validation_have_a_fixed_order(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = _enabled_harness(app_harness_factory)
    malformed_json = b'{"scenario":'

    with harness.client(source_ip="198.51.100.41") as anonymous:
        unauthenticated = anonymous.post(
            SIMULATOR_PATH,
            content=malformed_json,
            headers={"Content-Type": "application/json"},
        )

    with harness.client(source_ip="198.51.100.42") as agent_client:
        agent = bootstrap_workspace(agent_client, role="agent")
        assert agent.status_code == 201
        wrong_role = agent_client.post(
            SIMULATOR_PATH,
            content=malformed_json,
            headers={"Content-Type": "application/json"},
        )

    with harness.client(source_ip="198.51.100.43") as manager_client:
        manager = bootstrap_workspace(manager_client, role="manager")
        assert manager.status_code == 201
        csrf_token = manager.json()["csrf_token"]
        rejected_origin = manager_client.post(
            SIMULATOR_PATH,
            content=malformed_json,
            headers=_simulator_headers(
                "wrong-csrf-token",
                origin="https://attacker.invalid",
            ),
        )
        rejected_csrf = manager_client.post(
            SIMULATOR_PATH,
            content=malformed_json,
            headers=_simulator_headers("wrong-csrf-token"),
        )
        invalid_json = manager_client.post(
            SIMULATOR_PATH,
            content=malformed_json,
            headers=_simulator_headers(csrf_token),
        )
        invalid_scenario = _post_envelope(
            manager_client,
            csrf_token=csrf_token,
            scenario="future",
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "Authentication required"}
    assert wrong_role.status_code == 403
    assert wrong_role.json() == {"detail": "Insufficient role"}
    assert rejected_origin.status_code == 403
    assert rejected_origin.json() == {"detail": "Origin rejected"}
    assert rejected_csrf.status_code == 403
    assert rejected_csrf.json() == {"detail": "CSRF rejected"}
    assert invalid_json.status_code == 422
    assert invalid_json.json() == VALIDATION_FAILED
    assert invalid_scenario.status_code == 422
    assert invalid_scenario.json() == VALIDATION_FAILED


def test_global_body_limit_precedes_simulator_authentication(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    limit = 16 * 1024
    harness = _enabled_harness(
        app_harness_factory,
        api_max_request_body_bytes=limit,
    )

    with harness.client() as client:
        response = client.post(
            SIMULATOR_PATH,
            content=b"x" * (limit + 1),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == BODY_TOO_LARGE


def test_fresh_envelope_uses_exact_server_owned_bytes_and_real_cookie_free_ingress(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = _enabled_harness(app_harness_factory)

    with harness.client(source_ip="198.51.100.51") as manager_client:
        manager = bootstrap_workspace(manager_client, role="manager")
        assert manager.status_code == 201
        organization_id = manager.json()["workspace"]["id"]
        response = _post_envelope(
            manager_client,
            csrf_token=manager.json()["csrf_token"],
            scenario="fresh",
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    envelope = response.json()
    assert set(envelope) == {"path", "body", "timestamp", "event_id", "signature"}

    integration_id, key_version = _integration_for(
        harness,
        organization_id=organization_id,
    )
    assert envelope["path"] == f"/api/webhooks/synthetic/{integration_id}"
    assert envelope["timestamp"] == str(int(harness.clock().timestamp()))
    assert re.fullmatch(r"evt_[A-Za-z0-9]{8,64}", envelope["event_id"])
    assert re.fullmatch(r"v1=[0-9a-f]{64}", envelope["signature"])

    document = json.loads(envelope["body"])
    assert set(document) == {"type", "occurred_at", "data"}
    assert document["type"] == "payment.failed"
    assert document["occurred_at"] == "2026-10-07T12:00:00Z"
    assert set(document["data"]) == {"order"}
    order = document["data"]["order"]
    assert set(order) == {"id", "number", "amount_minor", "currency"}
    assert re.fullmatch(r"syn_order_[A-Za-z0-9]{8,48}", order["id"])
    assert re.fullmatch(r"DEMO-[0-9]{4,10}", order["number"])
    assert order["amount_minor"] == 12_900
    assert order["currency"] == "USD"
    assert envelope["body"] == json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )

    raw_body = envelope["body"].encode("ascii")
    integration_key = derive_webhook_integration_key(
        MASTER_SECRET.encode("utf-8"),
        integration_id=integration_id,
        key_version=key_version,
    )
    assert verify_webhook_signature(
        provided_signature=bytes.fromhex(envelope["signature"].removeprefix("v1=")),
        integration_key=integration_key,
        timestamp=int(envelope["timestamp"]),
        integration_id=integration_id,
        event_id=envelope["event_id"],
        raw_body=raw_body,
    )
    assert _business_counts(harness, organization_id=organization_id) == (0, 0)

    with harness.client(source_ip="198.51.100.52") as provider_client:
        assert not provider_client.cookies
        delivered = provider_client.post(
            envelope["path"],
            content=raw_body,
            headers=_delivery_headers(envelope),
        )

    assert delivered.status_code == 201
    assert delivered.json()["event_id"] == envelope["event_id"]
    assert delivered.json()["replayed"] is False
    assert _business_counts(harness, organization_id=organization_id) == (1, 1)


def test_stale_envelope_is_exactly_301_seconds_old_and_cannot_write_business_data(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = _enabled_harness(app_harness_factory)

    with harness.client(source_ip="198.51.100.61") as manager_client:
        manager = bootstrap_workspace(manager_client, role="manager")
        assert manager.status_code == 201
        organization_id = manager.json()["workspace"]["id"]
        response = _post_envelope(
            manager_client,
            csrf_token=manager.json()["csrf_token"],
            scenario="stale",
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    envelope = response.json()
    assert int(envelope["timestamp"]) == int(harness.clock().timestamp()) - 301
    assert json.loads(envelope["body"])["occurred_at"] == "2026-10-07T12:00:00Z"

    with harness.client(source_ip="198.51.100.62") as provider_client:
        rejected = provider_client.post(
            envelope["path"],
            content=envelope["body"].encode("ascii"),
            headers=_delivery_headers(envelope),
        )

    assert rejected.status_code == 401
    assert rejected.json() == WEBHOOK_AUTHENTICATION_FAILED
    assert _business_counts(harness, organization_id=organization_id) == (0, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"scenario": "fresh", "integration_id": "attacker-selected-target"},
        {"scenario": "fresh", "event_id": "evt_ATTACKERSELECTED"},
        {"scenario": "fresh", "body": "attacker-selected-body"},
        {"scenario": "fresh", "timestamp": "1", "signature": "v1=attacker"},
    ],
)
def test_client_cannot_choose_any_signed_envelope_field(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
    payload: dict[str, str],
) -> None:
    harness = _enabled_harness(app_harness_factory)

    with harness.client() as client:
        manager = bootstrap_workspace(client, role="manager")
        assert manager.status_code == 201
        response = client.post(
            SIMULATOR_PATH,
            headers=_simulator_headers(manager.json()["csrf_token"]),
            json=payload,
        )

    assert response.status_code == 422
    assert response.json() == VALIDATION_FAILED
    for value in payload.values():
        if value != "fresh":
            assert value not in response.text


def test_each_manager_receives_only_their_own_integration_path(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = _enabled_harness(app_harness_factory)

    with harness.client(source_ip="198.51.100.71") as first_client:
        first = bootstrap_workspace(first_client, role="manager")
        assert first.status_code == 201
        first_envelope = _post_envelope(
            first_client,
            csrf_token=first.json()["csrf_token"],
            scenario="fresh",
        ).json()

    with harness.client(source_ip="198.51.100.72") as second_client:
        second = bootstrap_workspace(second_client, role="manager")
        assert second.status_code == 201
        second_envelope = _post_envelope(
            second_client,
            csrf_token=second.json()["csrf_token"],
            scenario="fresh",
        ).json()

    first_integration, _ = _integration_for(
        harness,
        organization_id=first.json()["workspace"]["id"],
    )
    second_integration, _ = _integration_for(
        harness,
        organization_id=second.json()["workspace"]["id"],
    )
    assert first_integration != second_integration
    assert first_envelope["path"] == f"/api/webhooks/synthetic/{first_integration}"
    assert second_envelope["path"] == f"/api/webhooks/synthetic/{second_integration}"


def test_disabled_workspace_integration_is_not_available_to_the_simulator(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = _enabled_harness(app_harness_factory)

    with harness.client() as client:
        manager = bootstrap_workspace(client, role="manager")
        assert manager.status_code == 201
        organization_id = manager.json()["workspace"]["id"]
        with _session_factory(harness).begin() as database:
            integration = database.scalar(
                select(WebhookIntegration).where(
                    WebhookIntegration.organization_id == organization_id
                )
            )
            assert integration is not None
            integration.enabled = False

        response = _post_envelope(
            client,
            csrf_token=manager.json()["csrf_token"],
            scenario="fresh",
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Resource not found"}


def test_signing_material_is_returned_only_in_the_envelope_and_never_logged(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = _enabled_harness(
        app_harness_factory,
        webhook_master_secret=MASTER_SECRET_CANARY,
    )

    with harness.client() as client:
        manager = bootstrap_workspace(client, role="manager")
        assert manager.status_code == 201
        organization_id = manager.json()["workspace"]["id"]
        integration_id, key_version = _integration_for(
            harness,
            organization_id=organization_id,
        )
        derived_key_hex = derive_webhook_integration_key(
            MASTER_SECRET_CANARY.encode("utf-8"),
            integration_id=integration_id,
            key_version=key_version,
        ).hex()
        caplog.set_level(logging.DEBUG)
        caplog.clear()
        capsys.readouterr()

        response = _post_envelope(
            client,
            csrf_token=manager.json()["csrf_token"],
            scenario="fresh",
        )

    captured = capsys.readouterr()
    assert response.status_code == 200
    envelope = response.json()
    assert MASTER_SECRET_CANARY not in response.text
    assert derived_key_hex not in response.text

    log_sinks = (caplog.text, captured.out, captured.err)
    for sink in log_sinks:
        assert MASTER_SECRET_CANARY not in sink
        assert derived_key_hex not in sink
        assert envelope["body"] not in sink
        assert envelope["signature"] not in sink
