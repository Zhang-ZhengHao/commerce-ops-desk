"""Role-switch commands are safely retryable across session rotation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest
from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient

from app.services.idempotent_commands import canonical_json_digest


def test_command_payload_digest_uses_canonical_json() -> None:
    first = {"role": "agent", "nested": {"enabled": True, "count": 2}}
    reordered = {"nested": {"count": 2, "enabled": True}, "role": "agent"}

    assert canonical_json_digest(first) == canonical_json_digest(reordered)
    assert canonical_json_digest(first) != canonical_json_digest(
        {"role": "agent", "nested": {"enabled": True, "count": "2"}}
    )


def _command_headers(csrf_token: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": idempotency_key,
    }


def _synchronize_initial_receipt_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold both requests after their empty receipt read to expose the write race."""

    from app.api import demo as demo_api

    original_find = demo_api.find_command_receipt
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

    monkeypatch.setattr(demo_api, "find_command_receipt", synchronized_find)


def _send_concurrent_role_switches(
    app_harness: AppHarness,
    *,
    original_cookie: str,
    original_csrf: str,
    idempotency_keys: tuple[str, str],
) -> list[Any]:
    def send(request_number: int, idempotency_key: str) -> Any:
        threaded_client = app_harness.client(
            source_ip=f"198.51.100.{request_number + 20}",
            raise_server_exceptions=False,
        )
        try:
            threaded_client.cookies.set("commerce_ops_session", original_cookie)
            return threaded_client.post(
                "/api/demo/role",
                headers=_command_headers(original_csrf, idempotency_key),
                json={"role": "agent"},
            )
        finally:
            threaded_client.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(send, request_number, idempotency_key)
            for request_number, idempotency_key in enumerate(idempotency_keys)
        ]
        return [future.result(timeout=15) for future in futures]


def _database_command_state(
    database_path: Path,
    *,
    organization_id: str,
    original_membership_id: str,
) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        receipt_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM command_receipts
            WHERE membership_id = ? AND command_type = 'demo.role.switch'
            """,
            (original_membership_id,),
        ).fetchone()
        active_session_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM sessions
            WHERE organization_id = ? AND revoked_at IS NULL
            """,
            (organization_id,),
        ).fetchone()

    assert receipt_count is not None
    assert active_session_count is not None
    return int(receipt_count[0]), int(active_session_count[0])


@pytest.mark.parametrize("key", [None, "", "   ", "x" * 257])
def test_role_switch_rejects_a_missing_or_invalid_idempotency_key(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    key: str | None,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    headers = {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": bootstrap.json()["csrf_token"],
    }
    if key is not None:
        headers["Idempotency-Key"] = key

    response = client.post(
        "/api/demo/role",
        headers=headers,
        json={"role": "agent"},
    )

    assert response.status_code == 400


def test_same_rotated_session_command_can_be_replayed_but_not_changed(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    original_cookie = bootstrap.cookies["commerce_ops_session"]
    original_csrf = bootstrap.json()["csrf_token"]
    idempotency_key = "switch-to-agent-after-demo-bootstrap"
    headers = _command_headers(original_csrf, idempotency_key)

    first = client.post(
        "/api/demo/role",
        headers=headers,
        json={"role": "agent"},
    )
    assert first.status_code == 200
    first_replacement_cookie = first.cookies["commerce_ops_session"]

    with sqlite3.connect(app_harness.database_path) as connection:
        stored_response = connection.execute(
            "SELECT response_json FROM command_receipts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    assert stored_response is not None
    assert original_cookie not in stored_response[0]
    assert first_replacement_cookie not in stored_response[0]
    assert original_csrf not in stored_response[0]
    assert first.json()["csrf_token"] not in stored_response[0]

    with app_harness.client() as replay_client:
        replay_client.cookies.set("commerce_ops_session", original_cookie)
        replay = replay_client.post(
            "/api/demo/role",
            headers=headers,
            json={"role": "agent"},
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert replay.json()["csrf_token"] != original_csrf
        assert replay.cookies["commerce_ops_session"] == first_replacement_cookie
        assert replay_client.get("/api/session").status_code == 200

    assert client.get("/api/session").status_code == 200

    with app_harness.client() as changed_payload_client:
        changed_payload_client.cookies.set("commerce_ops_session", original_cookie)
        changed_payload = changed_payload_client.post(
            "/api/demo/role",
            headers=headers,
            json={"role": "manager"},
        )

    with app_harness.client() as different_key_client:
        different_key_client.cookies.set("commerce_ops_session", original_cookie)
        different_key = different_key_client.post(
            "/api/demo/role",
            headers=_command_headers(original_csrf, "a-new-command-key"),
            json={"role": "agent"},
        )

    assert changed_payload.status_code == 409
    assert different_key.status_code == 401

    receipt_count, active_session_count = _database_command_state(
        app_harness.database_path,
        organization_id=bootstrap.json()["workspace"]["id"],
        original_membership_id=bootstrap.json()["identity"]["membership_id"],
    )
    assert receipt_count == 1
    assert active_session_count == 1


def test_lost_role_switch_response_replays_identically_after_an_app_restart(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    first_process = app_harness_factory()
    idempotency_key = "restart-safe-role-switch"

    with first_process.client() as first_client:
        bootstrap = first_client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "role-restart-bootstrap",
            },
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201
        original_cookie = bootstrap.cookies["commerce_ops_session"]
        original_csrf = bootstrap.json()["csrf_token"]

        first_response = first_client.post(
            "/api/demo/role",
            headers=_command_headers(original_csrf, idempotency_key),
            json={"role": "agent"},
        )
        assert first_response.status_code == 200
        expected_body = first_response.json()
        expected_replacement_cookie = first_response.cookies["commerce_ops_session"]

    restarted_process = app_harness_factory(
        database_path=first_process.database_path,
        session_secret=first_process.settings.session_secret,
    )
    with restarted_process.client() as replay_client:
        replay_client.cookies.set("commerce_ops_session", original_cookie)
        replay = replay_client.post(
            "/api/demo/role",
            headers=_command_headers(original_csrf, idempotency_key),
            json={"role": "agent"},
        )

        assert replay.status_code == 200
        assert replay.json() == expected_body
        assert replay.cookies["commerce_ops_session"] == expected_replacement_cookie
        assert replay_client.get("/api/session").status_code == 200


def test_same_role_no_op_does_not_reserve_the_idempotency_key(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    idempotency_key = "same-manager-membership-retry"

    first = client.post(
        "/api/demo/role",
        headers=_command_headers(bootstrap.json()["csrf_token"], idempotency_key),
        json={"role": "manager"},
    )
    assert first.status_code == 200
    expected_body = first.json()
    expected_cookie = first.cookies["commerce_ops_session"]

    replay = client.post(
        "/api/demo/role",
        headers=_command_headers(first.json()["csrf_token"], idempotency_key),
        json={"role": "manager"},
    )

    assert replay.status_code == 200
    assert replay.json() == expected_body
    assert replay.cookies["commerce_ops_session"] == expected_cookie

    changed_payload = client.post(
        "/api/demo/role",
        headers=_command_headers(replay.json()["csrf_token"], idempotency_key),
        json={"role": "agent"},
    )
    assert changed_payload.status_code == 200
    assert changed_payload.json()["identity"]["role"] == "agent"


def test_cross_role_replacement_cookie_replays_without_new_database_records(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    idempotency_key = "manager-to-agent-retry-with-agent-cookie"

    first = client.post(
        "/api/demo/role",
        headers=_command_headers(bootstrap.json()["csrf_token"], idempotency_key),
        json={"role": "agent"},
    )
    assert first.status_code == 200
    expected_body = first.json()
    expected_cookie = first.cookies["commerce_ops_session"]

    with sqlite3.connect(app_harness.database_path) as connection:
        counts_before_retry = (
            connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone(),
            connection.execute("SELECT COUNT(*) FROM sessions").fetchone(),
        )

    replay = client.post(
        "/api/demo/role",
        headers=_command_headers(first.json()["csrf_token"], idempotency_key),
        json={"role": "agent"},
    )
    changed_payload = client.post(
        "/api/demo/role",
        headers=_command_headers(replay.json()["csrf_token"], idempotency_key),
        json={"role": "manager"},
    )

    with sqlite3.connect(app_harness.database_path) as connection:
        counts_after_retries = (
            connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone(),
            connection.execute("SELECT COUNT(*) FROM sessions").fetchone(),
        )

    assert replay.status_code == 200
    assert counts_after_retries == counts_before_retry
    assert replay.json() == expected_body
    assert replay.cookies["commerce_ops_session"] == expected_cookie
    assert changed_payload.status_code == 409


def test_concurrent_same_key_role_switch_has_one_receipt_and_one_active_replacement(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    body = bootstrap.json()
    _synchronize_initial_receipt_reads(monkeypatch)

    responses = _send_concurrent_role_switches(
        app_harness,
        original_cookie=bootstrap.cookies["commerce_ops_session"],
        original_csrf=body["csrf_token"],
        idempotency_keys=("concurrent-same-command", "concurrent-same-command"),
    )

    receipt_count, active_session_count = _database_command_state(
        app_harness.database_path,
        organization_id=body["workspace"]["id"],
        original_membership_id=body["identity"]["membership_id"],
    )
    statuses = [response.status_code for response in responses]

    assert (statuses, receipt_count, active_session_count) == ([200, 200], 1, 1)
    assert responses[0].json() == responses[1].json()
    response_cookies = [response.cookies["commerce_ops_session"] for response in responses]
    assert response_cookies[0] == response_cookies[1]
    for response_cookie in response_cookies:
        validation_client = app_harness.client()
        try:
            validation_client.cookies.set("commerce_ops_session", response_cookie)
            assert validation_client.get("/api/session").status_code == 200
        finally:
            validation_client.close()


def test_concurrent_different_keys_cannot_both_replace_the_same_old_session(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    body = bootstrap.json()
    _synchronize_initial_receipt_reads(monkeypatch)

    responses = _send_concurrent_role_switches(
        app_harness,
        original_cookie=bootstrap.cookies["commerce_ops_session"],
        original_csrf=body["csrf_token"],
        idempotency_keys=("concurrent-command-a", "concurrent-command-b"),
    )

    receipt_count, active_session_count = _database_command_state(
        app_harness.database_path,
        organization_id=body["workspace"]["id"],
        original_membership_id=body["identity"]["membership_id"],
    )
    statuses = sorted(response.status_code for response in responses)

    assert (statuses, receipt_count, active_session_count) == ([200, 401], 1, 1)
