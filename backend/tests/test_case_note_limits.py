"""Demo workspaces bound note growth without charging exact retries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from typing import Any

import pytest
from conftest import SAME_ORIGIN, AppHarness


def _headers(csrf_token: str, key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": key,
    }


def _synchronize_initial_receipt_reads(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_note_limit_replays_the_last_success_without_another_charge(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_case_note_limit=1)
    with harness.client() as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN, "Idempotency-Key": "note-limit-bootstrap"},
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201
        target = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
        first_command = {"body": "First bounded note.", "version": target["version"]}
        first_headers = _headers(bootstrap.json()["csrf_token"], "bounded-note-1")

        first = client.post(
            f"/api/cases/{target['id']}/notes",
            headers=first_headers,
            json=first_command,
        )
        replay = client.post(
            f"/api/cases/{target['id']}/notes",
            headers=first_headers,
            json=first_command,
        )
        limited = client.post(
            f"/api/cases/{target['id']}/notes",
            headers=_headers(bootstrap.json()["csrf_token"], "bounded-note-2"),
            json={"body": "This note exceeds the limit.", "version": 2},
        )
        detail = client.get(f"/api/cases/{target['id']}")

    expected = {"case_id": target["id"], "version": 2}
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == expected
    assert replay.json() == expected
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Demo workspace case note limit exceeded"
    assert detail.status_code == 200
    assert detail.json()["version"] == 2
    assert [note["body"] for note in detail.json()["notes"]] == ["First bounded note."]
    assert [event["action"] for event in detail.json()["audit_events"]] == ["case.note_added"]

    with sqlite3.connect(harness.database_path) as database:
        counter = database.execute("SELECT case_note_count FROM organizations").fetchone()
        note_count = database.execute("SELECT COUNT(*) FROM case_notes").fetchone()
        audit_count = database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'case.note_added'"
        ).fetchone()
        receipt_count = database.execute(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type = 'case.note'"
        ).fetchone()

    assert counter == (1,)
    assert note_count == (1,)
    assert audit_count == (1,)
    assert receipt_count == (1,)


def test_concurrent_exact_note_at_the_limit_replays_one_charge(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = app_harness_factory(demo_case_note_limit=1)
    with harness.client() as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "exact-note-limit-bootstrap",
            },
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201
        target = client.get("/api/cases", params={"rule_key": "refund_review"}).json()["items"][0]
    _synchronize_initial_receipt_reads(monkeypatch)

    def send(request_number: int) -> Any:
        with harness.client(
            source_ip=f"198.51.100.{request_number + 170}",
            raise_server_exceptions=False,
        ) as threaded_client:
            threaded_client.cookies.set(
                "commerce_ops_session",
                bootstrap.cookies["commerce_ops_session"],
            )
            return threaded_client.post(
                f"/api/cases/{target['id']}/notes",
                headers=_headers(
                    bootstrap.json()["csrf_token"],
                    "concurrent-exact-bounded-note",
                ),
                json={"body": "One exact bounded note.", "version": target["version"]},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    expected = {"case_id": target["id"], "version": target["version"] + 1}
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [expected, expected]
    with sqlite3.connect(harness.database_path) as database:
        counts = database.execute(
            """
            SELECT
                organizations.case_note_count,
                (SELECT COUNT(*) FROM case_notes),
                (SELECT COUNT(*) FROM audit_events WHERE action = 'case.note_added'),
                (SELECT COUNT(*) FROM command_receipts WHERE command_type = 'case.note')
            FROM organizations
            """
        ).fetchone()
    assert counts == (1, 1, 1, 1)


def test_concurrent_notes_cannot_cross_the_workspace_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_case_note_limit=1)
    with harness.client() as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "concurrent-note-limit-bootstrap",
            },
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201
        targets = [
            case for case in client.get("/api/cases").json()["items"] if case["status"] == "open"
        ][:2]

    start = Barrier(2)

    def send(request_number: int) -> Any:
        target = targets[request_number]
        with harness.client(
            source_ip=f"198.51.100.{request_number + 160}",
            raise_server_exceptions=False,
        ) as threaded_client:
            threaded_client.cookies.set(
                "commerce_ops_session",
                bootstrap.cookies["commerce_ops_session"],
            )
            start.wait(timeout=10)
            return threaded_client.post(
                f"/api/cases/{target['id']}/notes",
                headers=_headers(
                    bootstrap.json()["csrf_token"],
                    f"concurrent-bounded-note-{request_number}",
                ),
                json={
                    "body": f"Concurrent bounded note {request_number}.",
                    "version": target["version"],
                },
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    assert sorted(response.status_code for response in responses) == [200, 429]
    limited = next(response for response in responses if response.status_code == 429)
    assert limited.json()["detail"] == "Demo workspace case note limit exceeded"

    with sqlite3.connect(harness.database_path) as database:
        counter = database.execute("SELECT case_note_count FROM organizations").fetchone()
        note_count = database.execute("SELECT COUNT(*) FROM case_notes").fetchone()
        audit_count = database.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'case.note_added'"
        ).fetchone()
        receipt_count = database.execute(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type = 'case.note'"
        ).fetchone()
        changed_count = database.execute(
            """
            SELECT COUNT(*)
            FROM exception_cases
            WHERE id IN (?, ?) AND version = 2
            """,
            (targets[0]["id"], targets[1]["id"]),
        ).fetchone()

    assert counter == (1,)
    assert note_count == (1,)
    assert audit_count == (1,)
    assert receipt_count == (1,)
    assert changed_count == (1,)
