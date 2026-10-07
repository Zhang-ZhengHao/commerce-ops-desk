"""Audit history is append-only, tenant scoped, and role scoped."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from conftest import SAME_ORIGIN
from fastapi.testclient import TestClient


def test_manager_can_page_the_workspace_audit_history(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    manager = bootstrap.json()["identity"]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    target = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    assigned = client.post(
        f"/api/cases/{target['id']}/assignment",
        headers={
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": bootstrap.json()["csrf_token"],
            "Idempotency-Key": "audit-list-assignment",
        },
        json={"assignee_id": agent_id, "version": target["version"]},
    )
    assert assigned.status_code == 200

    response = client.get("/api/audit-events", params={"case_id": target["id"]})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["page"] == 1
    assert response.json()["page_size"] == 50
    event = response.json()["items"][0]
    assert event["action"] == "case.assigned"
    assert event["object_type"] == "case"
    assert event["object_id"] == target["id"]
    assert event["actor"] == {
        "membership_id": manager["membership_id"],
        "display_name": "Demo Manager",
    }
    assert event["changes"]["to_assignee_id"] == agent_id


def test_agent_audit_history_is_limited_to_owned_cases(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    manager = bootstrap_workspace(client, role="manager")
    assert manager.status_code == 201
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    queue = client.get("/api/cases").json()["items"]
    own_case = next(item for item in queue if item["order"]["order_number"] == "DEMO-1043")
    hidden_case = next(item for item in queue if item["order"]["order_number"] == "DEMO-1044")
    assigned = client.post(
        f"/api/cases/{own_case['id']}/assignment",
        headers={
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": manager.json()["csrf_token"],
            "Idempotency-Key": "audit-owned-reassignment",
        },
        json={"assignee_id": agent_id, "version": own_case["version"]},
    )
    assert assigned.status_code == 200
    agent = client.post(
        "/api/demo/role",
        headers={
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": manager.json()["csrf_token"],
            "Idempotency-Key": "audit-switch-to-agent",
        },
        json={"role": "agent"},
    )
    assert agent.status_code == 200

    visible = client.get("/api/audit-events")
    hidden = client.get("/api/audit-events", params={"case_id": hidden_case["id"]})

    assert visible.status_code == 200
    assert visible.json()["total"] == 1
    assert visible.json()["items"][0]["object_id"] == own_case["id"]
    assert hidden.status_code == 404


def test_case_audit_order_follows_persisted_case_versions_when_timestamps_match(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories import audit as audit_repository

    generated_ids = iter(
        (
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            UUID("88888888-8888-8888-8888-888888888888"),
            UUID("00000000-0000-0000-0000-000000000000"),
        )
    )
    monkeypatch.setattr(audit_repository, "uuid4", lambda: next(generated_ids))

    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    csrf_token = bootstrap.json()["csrf_token"]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    target = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]

    def headers(key: str) -> dict[str, str]:
        return {
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": key,
        }

    assigned = client.post(
        f"/api/cases/{target['id']}/assignment",
        headers=headers("ordered-audit-assignment"),
        json={"assignee_id": agent_id, "version": target["version"]},
    )
    noted = client.post(
        f"/api/cases/{target['id']}/notes",
        headers=headers("ordered-audit-note"),
        json={"body": "Persist the causal order.", "version": assigned.json()["version"]},
    )
    resolved = client.post(
        f"/api/cases/{target['id']}/resolution",
        headers=headers("ordered-audit-resolution"),
        json={"reason": "refund_approved", "version": noted.json()["version"]},
    )

    assert assigned.status_code == 200
    assert noted.status_code == 200
    assert resolved.status_code == 200
    history = client.get("/api/audit-events", params={"case_id": target["id"]})
    detail = client.get(f"/api/cases/{target['id']}")

    assert history.status_code == 200
    assert [event["action"] for event in history.json()["items"]] == [
        "case.resolved",
        "case.note_added",
        "case.assigned",
    ]
    assert [event["action"] for event in detail.json()["audit_events"]] == [
        "case.assigned",
        "case.note_added",
        "case.resolved",
    ]
