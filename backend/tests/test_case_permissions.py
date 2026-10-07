"""Record-level authorization and write boundaries cannot be delegated to the UI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient


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
