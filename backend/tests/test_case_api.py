"""HTTP contracts for the first order/case workflow slice."""

from collections.abc import Callable
from typing import Any

from conftest import SAME_ORIGIN
from fastapi.testclient import TestClient


def _command_headers(csrf_token: str, key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": key,
    }


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
