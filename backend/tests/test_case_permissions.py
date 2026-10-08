"""Record-level authorization and write boundaries cannot be delegated to the UI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.webhook_event import SyntheticWebhookEventPayload
from app.models import ExceptionCase, WebhookEvent, WebhookIntegration
from app.services.webhook_processing import process_payment_failed_webhook


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
    }


def _switch_role(client: TestClient, *, csrf: str, role: str, key: str) -> Any:
    return client.post(
        "/api/demo/role",
        headers=_headers(csrf, key),
        json={"role": role},
    )


def _create_unassigned_webhook_case(harness: AppHarness) -> str:
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    payload = SyntheticWebhookEventPayload.model_validate(
        {
            "type": "payment.failed",
            "occurred_at": "2026-10-07T11:59:00Z",
            "data": {
                "order": {
                    "id": "syn_order_PERMISSION01",
                    "number": "DEMO-3045",
                    "amount_minor": 8700,
                    "currency": "USD",
                }
            },
        }
    )
    with factory() as database:
        organization_id, integration_id, key_version = database.execute(
            select(
                WebhookIntegration.organization_id,
                WebhookIntegration.id,
                WebhookIntegration.key_version,
            )
        ).one()
        result = process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            integration_key_version=key_version,
            external_event_id="evt_PERMISSION01",
            payload_digest="ab" * 32,
            payload=payload,
            event_limit=20,
            received_at=harness.clock(),
        )
    return result.case_id


def test_agent_cannot_read_or_write_an_unassigned_case(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    manager = bootstrap_workspace(client, role="manager")
    assert manager.status_code == 201
    unassigned = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    agent = _switch_role(
        client,
        csrf=manager.json()["csrf_token"],
        role="agent",
        key="permission-switch-to-agent",
    )
    assert agent.status_code == 200
    headers = _headers(agent.json()["csrf_token"], "agent-must-not-touch-unowned")

    assert client.get(f"/api/cases/{unassigned['id']}").status_code == 404
    assert (
        client.post(
            f"/api/cases/{unassigned['id']}/notes",
            headers=headers,
            json={"body": "This must stay invisible.", "version": unassigned["version"]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/cases/{unassigned['id']}/resolution",
            headers={**headers, "Idempotency-Key": "agent-unowned-resolution"},
            json={"reason": "refund_approved", "version": unassigned["version"]},
        ).status_code
        == 404
    )
    own_agent_id = agent.json()["identity"]["membership_id"]
    assert (
        client.post(
            f"/api/cases/{unassigned['id']}/assignment",
            headers={**headers, "Idempotency-Key": "agent-forbidden-assignment"},
            json={"assignee_id": own_agent_id, "version": unassigned["version"]},
        ).status_code
        == 403
    )


def test_webhook_provenance_remains_inside_agent_assignment_visibility(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    manager = bootstrap_workspace(client, role="manager")
    assert manager.status_code == 201
    case_id = _create_unassigned_webhook_case(app_harness)

    agent = _switch_role(
        client,
        csrf=manager.json()["csrf_token"],
        role="agent",
        key="webhook-permission-switch-agent",
    )
    assert agent.status_code == 200
    assert client.get(f"/api/cases/{case_id}").status_code == 404
    assert case_id not in {item["id"] for item in client.get("/api/cases").json()["items"]}

    manager_again = _switch_role(
        client,
        csrf=agent.json()["csrf_token"],
        role="manager",
        key="webhook-permission-switch-manager",
    )
    assert manager_again.status_code == 200
    assigned = client.post(
        f"/api/cases/{case_id}/assignment",
        headers=_headers(
            manager_again.json()["csrf_token"],
            "webhook-permission-assignment",
        ),
        json={
            "assignee_id": agent.json()["identity"]["membership_id"],
            "version": 1,
        },
    )
    assert assigned.status_code == 200

    agent_again = _switch_role(
        client,
        csrf=manager_again.json()["csrf_token"],
        role="agent",
        key="webhook-permission-switch-agent-again",
    )
    assert agent_again.status_code == 200
    detail = client.get(f"/api/cases/{case_id}")

    assert detail.status_code == 200
    assert detail.json()["source"] == {
        "kind": "synthetic_webhook",
        "provider": "synthetic",
        "event_type": "payment.failed",
        "external_event_id": "evt_PERMISSION01",
        "received_at": "2026-10-07T12:00:00Z",
    }


def test_cross_organization_case_and_assignee_ids_are_not_found(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    first = bootstrap_workspace(client, role="manager")
    assert first.status_code == 201
    own_case = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]

    with app_harness.client(source_ip="198.51.100.80") as other_client:
        other = bootstrap_workspace(other_client, role="manager")
        assert other.status_code == 201
        other_case = other_client.get("/api/cases").json()["items"][0]
        other_agent = other_client.get("/api/agents").json()["items"][0]

    assert client.get(f"/api/cases/{other_case['id']}").status_code == 404
    assert (
        client.post(
            f"/api/cases/{other_case['id']}/assignment",
            headers=_headers(first.json()["csrf_token"], "cross-org-case"),
            json={"assignee_id": other_agent["membership_id"], "version": 1},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/cases/{own_case['id']}/assignment",
            headers=_headers(first.json()["csrf_token"], "cross-org-assignee"),
            json={"assignee_id": other_agent["membership_id"], "version": 1},
        ).status_code
        == 404
    )


def test_case_source_join_cannot_borrow_another_organizations_webhook_event(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    first = bootstrap_workspace(client, role="manager")
    assert first.status_code == 201
    own_case = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]

    with app_harness.client(source_ip="198.51.100.81") as other_client:
        other = bootstrap_workspace(other_client, role="manager")
        assert other.status_code == 201
        other_case = other_client.get(
            "/api/cases",
            params={"rule_key": "refund_review"},
        ).json()["items"][0]

    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    with factory() as database:
        own_source_event_id = database.scalar(
            select(ExceptionCase.source_event_id).where(ExceptionCase.id == own_case["id"])
        )
        other_case_row = database.scalar(
            select(ExceptionCase).where(ExceptionCase.id == other_case["id"])
        )
        assert own_source_event_id is not None
        assert other_case_row is not None
        integration = database.scalar(
            select(WebhookIntegration).where(
                WebhookIntegration.organization_id == other_case_row.organization_id
            )
        )
        assert integration is not None
        database.add(
            WebhookEvent(
                id=own_source_event_id,
                organization_id=other_case_row.organization_id,
                integration_id=integration.id,
                external_event_id="evt_OTHERTENANT01",
                event_type="payment.failed",
                payload_digest="cd" * 32,
                occurred_at=app_harness.clock(),
                order_id=other_case_row.order_id,
                case_id=other_case_row.id,
                received_at=app_harness.clock(),
                processed_at=app_harness.clock(),
            )
        )
        database.commit()

    detail = client.get(f"/api/cases/{own_case['id']}")

    assert detail.status_code == 200
    assert detail.json()["source"] == {"kind": "seeded_demo"}
    assert "evt_OTHERTENANT01" not in detail.text


def test_case_writes_require_csrf_and_an_idempotency_key(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    manager = bootstrap_workspace(client, role="manager")
    assert manager.status_code == 201
    case = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    command = {"assignee_id": agent_id, "version": case["version"]}

    missing_csrf = client.post(
        f"/api/cases/{case['id']}/assignment",
        headers={"Origin": SAME_ORIGIN, "Idempotency-Key": "missing-csrf"},
        json=command,
    )
    missing_key = client.post(
        f"/api/cases/{case['id']}/assignment",
        headers={
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": manager.json()["csrf_token"],
        },
        json=command,
    )

    assert missing_csrf.status_code == 403
    assert missing_key.status_code == 400


def test_stale_version_and_changed_idempotent_payload_return_conflict(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    manager = bootstrap_workspace(client, role="manager")
    assert manager.status_code == 201
    case = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    first = client.post(
        f"/api/cases/{case['id']}/assignment",
        headers=_headers(manager.json()["csrf_token"], "stable-assignment-key"),
        json={"assignee_id": agent_id, "version": case["version"]},
    )
    assert first.status_code == 200

    changed_payload = client.post(
        f"/api/cases/{case['id']}/assignment",
        headers=_headers(manager.json()["csrf_token"], "stable-assignment-key"),
        json={"assignee_id": agent_id, "version": first.json()["version"]},
    )
    stale_version = client.post(
        f"/api/cases/{case['id']}/assignment",
        headers=_headers(manager.json()["csrf_token"], "different-assignment-key"),
        json={"assignee_id": agent_id, "version": case["version"]},
    )

    assert changed_payload.status_code == 409
    assert changed_payload.json()["detail"] == "Idempotency key is already bound to another payload"
    assert stale_version.status_code == 409
    assert stale_version.json()["detail"] == "Case version conflict"


def test_resolution_reason_and_terminal_state_are_enforced(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    manager = bootstrap_workspace(client, role="manager")
    assert manager.status_code == 201
    case = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    csrf = manager.json()["csrf_token"]

    invalid_reason = client.post(
        f"/api/cases/{case['id']}/resolution",
        headers=_headers(csrf, "invalid-refund-resolution"),
        json={"reason": "payment_recovered", "version": case["version"]},
    )
    assert invalid_reason.status_code == 422

    resolved = client.post(
        f"/api/cases/{case['id']}/resolution",
        headers=_headers(csrf, "manager-direct-resolution"),
        json={"reason": "refund_approved", "version": case["version"]},
    )
    assert resolved.status_code == 200
    assert resolved.json() == {"case_id": case["id"], "version": case["version"] + 1}
    detail = client.get(f"/api/cases/{case['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "resolved"

    terminal_note = client.post(
        f"/api/cases/{case['id']}/notes",
        headers=_headers(csrf, "resolved-case-note"),
        json={"body": "Resolved cases are terminal.", "version": resolved.json()["version"]},
    )
    assert terminal_note.status_code == 409
