"""HTTP contracts for the first order/case workflow slice."""

from collections.abc import Callable
from typing import Any, cast

from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.webhook import derive_webhook_integration_key, sign_webhook_request
from app.models import WebhookEvent, WebhookIntegration

WEBHOOK_MASTER_SECRET = "case-provenance-test-webhook-secret"
WEBHOOK_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T11:59:00Z",'
    b'"data":{"order":{"id":"syn_order_PROVENANCE01","number":"DEMO-2045",'
    b'"amount_minor":12900,"currency":"USD"}}}'
)


def _command_headers(csrf_token: str, key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": key,
    }


def _deliver_webhook_case(
    client: TestClient,
    *,
    harness: AppHarness,
    event_id: str,
) -> Any:
    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        integration_id, key_version = database.execute(
            select(WebhookIntegration.id, WebhookIntegration.key_version)
        ).one()
    timestamp = int(harness.clock().timestamp())
    integration_key = derive_webhook_integration_key(
        WEBHOOK_MASTER_SECRET.encode("utf-8"),
        integration_id=integration_id,
        key_version=key_version,
    )
    return client.post(
        f"/api/webhooks/synthetic/{integration_id}",
        content=WEBHOOK_BODY,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": str(timestamp),
            "X-Webhook-Event-Id": event_id,
            "X-Webhook-Signature": sign_webhook_request(
                integration_key=integration_key,
                timestamp=timestamp,
                integration_id=integration_id,
                event_id=event_id,
                raw_body=WEBHOOK_BODY,
            ),
        },
    )


def test_manager_dashboard_summarizes_the_seeded_workspace(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    response = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.json() == {
        "generated_at": "2026-10-07T12:00:00Z",
        "summary": {
            "open": 3,
            "approaching_sla": 2,
            "high_severity": 2,
            "resolved": 1,
        },
        "by_rule": [
            {"rule_key": "fulfillment_delayed", "case_type": "fulfillment", "count": 1},
            {"rule_key": "payment_failed", "case_type": "payment", "count": 2},
            {"rule_key": "refund_review", "case_type": "refund", "count": 1},
        ],
    }


def test_agent_dashboard_contains_only_their_assigned_work(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="agent")
    assert bootstrap.status_code == 201

    response = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.json()["summary"] == {
        "open": 1,
        "approaching_sla": 1,
        "high_severity": 1,
        "resolved": 0,
    }
    assert response.json()["by_rule"] == [
        {"rule_key": "payment_failed", "case_type": "payment", "count": 1}
    ]


def test_manager_case_queue_filters_the_seeded_cases(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    response = client.get(
        "/api/cases",
        params={"status": "open", "severity": "high"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["rule_key"] == "fulfillment_delayed"
    assert item["case_type"] == "fulfillment"
    assert item["severity"] == "high"
    assert item["status"] == "open"
    assert item["version"] == 1
    assert item["order"] == {
        "id": item["order"]["id"],
        "order_number": "DEMO-1044",
        "amount_minor": 15_750,
        "currency": "USD",
        "payment_status": "paid",
        "fulfillment_status": "delayed",
    }
    assert item["assignee"] is None


def test_agent_can_open_only_the_seeded_case_assigned_to_them(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="agent")
    assert bootstrap.status_code == 201

    queue = client.get("/api/cases")
    assert queue.status_code == 200
    assert queue.json()["total"] == 1
    own_case = queue.json()["items"][0]
    assert own_case["order"]["order_number"] == "DEMO-1042"
    assert own_case["source"] == {"kind": "seeded_demo"}
    assert own_case["assignee"] == {
        "membership_id": bootstrap.json()["identity"]["membership_id"],
        "display_name": "Demo Agent",
    }

    detail = client.get(f"/api/cases/{own_case['id']}")

    assert detail.status_code == 200
    assert detail.json() == {
        **own_case,
        "created_at": "2026-10-07T12:00:00Z",
        "resolution_reasons": [
            "payment_recovered",
            "customer_contacted",
            "order_cancelled",
        ],
        "notes": [],
        "audit_events": [],
    }


def test_webhook_case_exposes_only_safe_source_in_queue_and_detail(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = app_harness_factory(
        webhook_enabled=True,
        webhook_master_secret=WEBHOOK_MASTER_SECRET,
    )
    event_id = "evt_PROVENANCE01"

    with harness.client() as client:
        bootstrap = bootstrap_workspace(client, role="manager")
        assert bootstrap.status_code == 201
        delivery = _deliver_webhook_case(client, harness=harness, event_id=event_id)
        assert delivery.status_code == 201
        case_id = delivery.json()["case_id"]

        queue = client.get("/api/cases")
        detail = client.get(f"/api/cases/{case_id}")

    assert queue.status_code == 200
    queue_case = next(item for item in queue.json()["items"] if item["id"] == case_id)
    assert detail.status_code == 200
    expected_source = {
        "kind": "synthetic_webhook",
        "provider": "synthetic",
        "event_type": "payment.failed",
        "external_event_id": event_id,
        "received_at": "2026-10-07T12:00:00Z",
    }
    assert queue_case["source"] == expected_source
    assert detail.json()["source"] == expected_source

    factory = cast(sessionmaker[Session], harness.app.state.session_factory)
    with factory() as database:
        internal_event_id, integration_id, payload_digest = database.execute(
            select(
                WebhookEvent.id,
                WebhookEvent.integration_id,
                WebhookEvent.payload_digest,
            ).where(WebhookEvent.case_id == case_id)
        ).one()
    serialized = detail.text
    assert internal_event_id not in serialized
    assert integration_id not in serialized
    assert payload_digest not in serialized


def test_manager_can_list_assignable_agents(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    response = client.get("/api/agents")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "membership_id": response.json()["items"][0]["membership_id"],
                "display_name": "Demo Agent",
            }
        ]
    }


def test_manager_assignment_is_versioned_audited_and_idempotent(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    csrf_token = bootstrap.json()["csrf_token"]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    queue = client.get("/api/cases", params={"rule_key": "refund_review"})
    target = queue.json()["items"][0]
    headers = _command_headers(csrf_token, "assign-refund-review")

    first = client.post(
        f"/api/cases/{target['id']}/assignment",
        headers=headers,
        json={"assignee_id": agent_id, "version": target["version"]},
    )
    replay = client.post(
        f"/api/cases/{target['id']}/assignment",
        headers=headers,
        json={"assignee_id": agent_id, "version": target["version"]},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json() == {"case_id": target["id"], "version": target["version"] + 1}
    detail = client.get(f"/api/cases/{target['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "assigned"
    assert detail.json()["version"] == target["version"] + 1
    assert detail.json()["assignee"] == {
        "membership_id": agent_id,
        "display_name": "Demo Agent",
    }
    assignment_audits = [
        event for event in detail.json()["audit_events"] if event["action"] == "case.assigned"
    ]
    assert len(assignment_audits) == 1
    assert assignment_audits[0]["changes"] == {
        "from_assignee_id": None,
        "from_status": "open",
        "to_assignee_id": agent_id,
        "to_status": "assigned",
        "version": 2,
    }


def test_agent_note_is_versioned_audited_and_idempotent(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="agent")
    assert bootstrap.status_code == 201
    target = client.get("/api/cases").json()["items"][0]
    headers = _command_headers(bootstrap.json()["csrf_token"], "agent-investigation-note")
    command = {"body": "Verified the synthetic payment retry.", "version": target["version"]}

    first = client.post(
        f"/api/cases/{target['id']}/notes",
        headers=headers,
        json=command,
    )
    replay = client.post(
        f"/api/cases/{target['id']}/notes",
        headers=headers,
        json=command,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json() == {"case_id": target["id"], "version": 2}
    detail = client.get(f"/api/cases/{target['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["notes"]) == 1
    assert detail.json()["notes"][0]["body"] == command["body"]
    assert detail.json()["notes"][0]["author"] == {
        "membership_id": bootstrap.json()["identity"]["membership_id"],
        "display_name": "Demo Agent",
    }
    assert [event["action"] for event in detail.json()["audit_events"]] == ["case.note_added"]


def test_agent_can_resolve_their_case_with_an_allowed_reason(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="agent")
    assert bootstrap.status_code == 201
    target = client.get("/api/cases").json()["items"][0]
    headers = _command_headers(bootstrap.json()["csrf_token"], "resolve-own-payment-case")
    command = {"reason": "payment_recovered", "version": target["version"]}

    first = client.post(
        f"/api/cases/{target['id']}/resolution",
        headers=headers,
        json=command,
    )
    replay = client.post(
        f"/api/cases/{target['id']}/resolution",
        headers=headers,
        json=command,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json() == {"case_id": target["id"], "version": 2}
    detail = client.get(f"/api/cases/{target['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "resolved"
    assert detail.json()["resolution_reason"] == "payment_recovered"
    assert detail.json()["resolved_at"] == "2026-10-07T12:00:00Z"
    assert [event["action"] for event in detail.json()["audit_events"]] == ["case.resolved"]
