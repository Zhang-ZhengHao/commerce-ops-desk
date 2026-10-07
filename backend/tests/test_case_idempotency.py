"""Case commands keep one durable effect under concurrent retries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from typing import Any

import pytest
from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient


def _command_headers(csrf_token: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": idempotency_key,
    }


def _synchronize_initial_receipt_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose the race after both requests observe an unused command key."""

    from app.services import case_commands

    original_find = case_commands.find_command_receipt  # type: ignore[attr-defined]
    empty_read_barrier = Barrier(2)
    counter_lock = Lock()
    empty_read_count = 0

    def synchronized_find(*args: Any, **kwargs: Any) -> Any:
        nonlocal empty_read_count
        receipt = original_find(*args, **kwargs)
        if receipt is not None:
            return receipt

        with counter_lock:
            empty_read_count += 1
            should_wait = empty_read_count <= 2
        if should_wait:
            empty_read_barrier.wait(timeout=10)
        return None

    monkeypatch.setattr(case_commands, "find_command_receipt", synchronized_find)


@pytest.mark.parametrize("command", ["assignment", "note", "resolution"])
def test_case_command_returns_one_compact_retry_stable_result(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    command: str,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    target = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    headers = _command_headers(bootstrap.json()["csrf_token"], f"compact-{command}")
    request_by_command = {
        "assignment": (
            "assignment",
            {"assignee_id": agent_id, "version": target["version"]},
        ),
        "note": (
            "notes",
            {"body": "Verified the synthetic refund evidence.", "version": target["version"]},
        ),
        "resolution": (
            "resolution",
            {"reason": "refund_approved", "version": target["version"]},
        ),
    }
    path_suffix, request_body = request_by_command[command]

    first = client.post(
        f"/api/cases/{target['id']}/{path_suffix}",
        headers=headers,
        json=request_body,
    )
    replay = client.post(
        f"/api/cases/{target['id']}/{path_suffix}",
        headers=headers,
        json=request_body,
    )

    expected = {"case_id": target["id"], "version": target["version"] + 1}
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == expected
    assert replay.json() == expected
    with sqlite3.connect(app_harness.database_path) as database:
        stored_response = database.execute(
            "SELECT response_json FROM command_receipts WHERE idempotency_key = ?",
            (f"compact-{command}",),
        ).fetchone()
    assert stored_response is not None
    assert json.loads(stored_response[0]) == expected


def test_assignment_to_current_agent_is_a_zero_write_no_op(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    target = client.get(
        "/api/cases",
        params={"rule_key": "payment_failed", "status": "assigned"},
    ).json()["items"][0]
    assert target["assignee"] is not None
    headers = _command_headers(bootstrap.json()["csrf_token"], "same-assignee-no-op")
    command = {
        "assignee_id": target["assignee"]["membership_id"],
        "version": target["version"],
    }

    first = client.post(
        f"/api/cases/{target['id']}/assignment",
        headers=headers,
        json=command,
    )
    replay = client.post(
        f"/api/cases/{target['id']}/assignment",
        headers=headers,
        json=command,
    )

    expected = {"case_id": target["id"], "version": target["version"]}
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == expected
    assert replay.json() == expected

    detail = client.get(f"/api/cases/{target['id']}").json()
    assert detail["version"] == target["version"]
    assert detail["updated_at"] == target["updated_at"]
    assert detail["audit_events"] == []
    with sqlite3.connect(app_harness.database_path) as database:
        receipt_count = database.execute(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type = 'case.assignment'"
        ).fetchone()
    assert receipt_count == (0,)


@pytest.mark.parametrize(
    ("command_type", "path_suffix", "request_fields", "audit_action"),
    [
        ("case.assignment", "assignment", "assignment", "case.assigned"),
        ("case.note", "notes", "note", "case.note_added"),
        ("case.resolution", "resolution", "resolution", "case.resolved"),
    ],
)
def test_concurrent_exact_case_command_replays_one_effect(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    command_type: str,
    path_suffix: str,
    request_fields: str,
    audit_action: str,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    target = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    payloads = {
        "assignment": {"assignee_id": agent_id, "version": target["version"]},
        "note": {"body": "One concurrent note.", "version": target["version"]},
        "resolution": {"reason": "refund_approved", "version": target["version"]},
    }
    payload = payloads[request_fields]
    _synchronize_initial_receipt_reads(monkeypatch)

    def send(request_number: int) -> Any:
        with app_harness.client(
            source_ip=f"198.51.100.{request_number + 150}",
            raise_server_exceptions=False,
        ) as threaded_client:
            threaded_client.cookies.set(
                "commerce_ops_session",
                bootstrap.cookies["commerce_ops_session"],
            )
            return threaded_client.post(
                f"/api/cases/{target['id']}/{path_suffix}",
                headers=_command_headers(
                    bootstrap.json()["csrf_token"],
                    f"concurrent-exact-{request_fields}",
                ),
                json=payload,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    expected = {"case_id": target["id"], "version": target["version"] + 1}
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [expected, expected]

    with sqlite3.connect(app_harness.database_path) as database:
        receipt_count = database.execute(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type = ?",
            (command_type,),
        ).fetchone()
        audit_count = database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = ?",
            (audit_action,),
        ).fetchone()

    detail = client.get(f"/api/cases/{target['id']}").json()
    assert receipt_count == (1,)
    assert audit_count == (1,)
    assert detail["version"] == target["version"] + 1
    if request_fields == "note":
        assert [note["body"] for note in detail["notes"]] == ["One concurrent note."]


@pytest.mark.parametrize(
    ("command_type", "path_suffix", "request_fields", "audit_action"),
    [
        ("case.assignment", "assignment", "assignment", "case.assigned"),
        ("case.note", "notes", "note", "case.note_added"),
        ("case.resolution", "resolution", "resolution", "case.resolved"),
    ],
)
def test_concurrent_changed_case_command_has_one_success_and_one_conflict(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    command_type: str,
    path_suffix: str,
    request_fields: str,
    audit_action: str,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    cases = client.get("/api/cases").json()["items"]
    targets = [case for case in cases if case["status"] == "open"][:2]
    agent_id = client.get("/api/agents").json()["items"][0]["membership_id"]
    _synchronize_initial_receipt_reads(monkeypatch)

    def send(request_number: int) -> Any:
        target = targets[request_number]
        resolution_reason = {
            "refund_review": "refund_approved",
            "fulfillment_delayed": "carrier_updated",
        }[target["rule_key"]]
        payloads = {
            "assignment": {"assignee_id": agent_id, "version": target["version"]},
            "note": {
                "body": f"Concurrent changed note {request_number}.",
                "version": target["version"],
            },
            "resolution": {"reason": resolution_reason, "version": target["version"]},
        }
        with app_harness.client(
            source_ip=f"198.51.100.{request_number + 140}",
            raise_server_exceptions=False,
        ) as threaded_client:
            threaded_client.cookies.set(
                "commerce_ops_session",
                bootstrap.cookies["commerce_ops_session"],
            )
            return threaded_client.post(
                f"/api/cases/{target['id']}/{path_suffix}",
                headers=_command_headers(
                    bootstrap.json()["csrf_token"],
                    f"concurrent-changed-{request_fields}",
                ),
                json=payloads[request_fields],
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"] == "Idempotency key is already bound to another payload"

    with sqlite3.connect(app_harness.database_path) as database:
        receipt_count = database.execute(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type = ?",
            (command_type,),
        ).fetchone()
        audit_count = database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = ?",
            (audit_action,),
        ).fetchone()
        changed_count = database.execute(
            """
            SELECT COUNT(*)
            FROM exception_cases
            WHERE id IN (?, ?) AND version = 2
            """,
            (targets[0]["id"], targets[1]["id"]),
        ).fetchone()

    assert receipt_count == (1,)
    assert audit_count == (1,)
    assert changed_count == (1,)
