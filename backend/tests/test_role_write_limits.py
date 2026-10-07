"""Role changes are no-op aware and persistently bounded per workspace."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from conftest import SAME_ORIGIN, AppHarness


def _bootstrap(client: Any, *, key: str) -> Any:
    return client.post(
        "/api/demo/workspaces",
        headers={"Origin": SAME_ORIGIN, "Idempotency-Key": key},
        json={"initial_role": "manager"},
    )


def _switch(client: Any, *, csrf: str, key: str, role: str) -> Any:
    return client.post(
        "/api/demo/role",
        headers={
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": key,
        },
        json={"role": role},
    )


def _write_counts(app_harness: AppHarness) -> tuple[int, int]:
    with sqlite3.connect(app_harness.database_path) as connection:
        receipt_count = connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone()
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
    assert receipt_count is not None
    assert session_count is not None
    return int(receipt_count[0]), int(session_count[0])


def test_role_write_limit_defaults_to_32(app_harness: AppHarness) -> None:
    assert app_harness.settings.demo_role_write_limit == 32


def test_switching_to_the_current_role_is_a_true_no_op(
    app_harness: AppHarness,
) -> None:
    with app_harness.client() as client:
        bootstrap = _bootstrap(client, key="no-op-bootstrap")
        assert bootstrap.status_code == 201
        counts_before = _write_counts(app_harness)

        no_op = _switch(
            client,
            csrf=bootstrap.json()["csrf_token"],
            key="current-role-no-op",
            role="manager",
        )

    assert no_op.status_code == 200
    assert no_op.json() == bootstrap.json()
    assert no_op.cookies["commerce_ops_session"] == bootstrap.cookies["commerce_ops_session"]
    assert _write_counts(app_harness) == counts_before


def test_actual_role_changes_stop_at_the_persistent_workspace_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_role_write_limit=2)
    with harness.client() as client:
        bootstrap = _bootstrap(client, key="bounded-role-bootstrap")
        first = _switch(
            client,
            csrf=bootstrap.json()["csrf_token"],
            key="bounded-role-change-1",
            role="agent",
        )
        second = _switch(
            client,
            csrf=first.json()["csrf_token"],
            key="bounded-role-change-2",
            role="manager",
        )
        limited = _switch(
            client,
            csrf=second.json()["csrf_token"],
            key="bounded-role-change-3",
            role="agent",
        )

    assert [first.status_code, second.status_code, limited.status_code] == [
        200,
        200,
        429,
    ]
    assert int(limited.headers["Retry-After"]) > 0
    assert _write_counts(harness) == (2, 3)


def test_role_write_limit_survives_an_app_restart(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    first_process = app_harness_factory(demo_role_write_limit=1)
    with first_process.client() as first_client:
        bootstrap = _bootstrap(first_client, key="persistent-role-bootstrap")
        changed = _switch(
            first_client,
            csrf=bootstrap.json()["csrf_token"],
            key="persistent-role-change-1",
            role="agent",
        )
    assert changed.status_code == 200

    restarted_process = app_harness_factory(
        database_path=first_process.database_path,
        session_secret=first_process.settings.session_secret,
        demo_role_write_limit=1,
    )
    with restarted_process.client() as restarted_client:
        restarted_client.cookies.set(
            "commerce_ops_session",
            changed.cookies["commerce_ops_session"],
        )
        limited = _switch(
            restarted_client,
            csrf=changed.json()["csrf_token"],
            key="persistent-role-change-2",
            role="manager",
        )

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    assert _write_counts(first_process) == (1, 2)


def test_exact_role_replay_does_not_consume_another_write(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_role_write_limit=1)
    with harness.client() as client:
        bootstrap = _bootstrap(client, key="replay-role-bootstrap")
        changed = _switch(
            client,
            csrf=bootstrap.json()["csrf_token"],
            key="replay-role-change",
            role="agent",
        )
        replay = _switch(
            client,
            csrf=changed.json()["csrf_token"],
            key="replay-role-change",
            role="agent",
        )
        limited = _switch(
            client,
            csrf=replay.json()["csrf_token"],
            key="new-role-change-after-replay",
            role="manager",
        )

    assert replay.status_code == 200
    assert replay.json() == changed.json()
    assert replay.cookies["commerce_ops_session"] == changed.cookies["commerce_ops_session"]
    assert limited.status_code == 429
    assert _write_counts(harness) == (1, 2)


def test_concurrent_role_changes_cannot_cross_the_workspace_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_role_write_limit=1)
    with harness.client() as client:
        bootstrap = _bootstrap(client, key="concurrent-role-bootstrap")
    assert bootstrap.status_code == 201
    original_cookie = bootstrap.cookies["commerce_ops_session"]
    original_csrf = bootstrap.json()["csrf_token"]
    start = Barrier(2)

    def send(request_number: int) -> Any:
        with harness.client(
            source_ip=f"198.51.100.{request_number + 110}",
            raise_server_exceptions=False,
        ) as threaded_client:
            threaded_client.cookies.set("commerce_ops_session", original_cookie)
            start.wait(timeout=10)
            return _switch(
                threaded_client,
                csrf=original_csrf,
                key=f"concurrent-role-change-{request_number}",
                role="agent",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    assert sorted(response.status_code for response in responses) == [200, 429]
    assert _write_counts(harness) == (1, 2)


def test_concurrent_same_key_replays_even_when_the_first_write_fills_the_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_role_write_limit=1)
    with harness.client() as client:
        bootstrap = _bootstrap(client, key="same-key-limit-bootstrap")
    assert bootstrap.status_code == 201
    original_cookie = bootstrap.cookies["commerce_ops_session"]
    original_csrf = bootstrap.json()["csrf_token"]
    start = Barrier(2)

    def send(_: int) -> Any:
        with harness.client(raise_server_exceptions=False) as threaded_client:
            threaded_client.cookies.set("commerce_ops_session", original_cookie)
            start.wait(timeout=10)
            return _switch(
                threaded_client,
                csrf=original_csrf,
                key="same-role-command-at-limit",
                role="agent",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert (
        responses[0].cookies["commerce_ops_session"] == responses[1].cookies["commerce_ops_session"]
    )
    assert _write_counts(harness) == (1, 2)
