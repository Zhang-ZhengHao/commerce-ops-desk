"""Workspace bootstrap is retry-safe before it consumes bounded capacity."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient


def _bootstrap(
    client: TestClient,
    *,
    key: str,
    role: str = "manager",
) -> Any:
    return client.post(
        "/api/demo/workspaces",
        headers={"Origin": SAME_ORIGIN, "Idempotency-Key": key},
        json={"initial_role": role},
    )


def _creation_counts(database_path: Path) -> tuple[int, int, int, int]:
    with sqlite3.connect(database_path) as connection:
        organization_count = connection.execute("SELECT COUNT(*) FROM organizations").fetchone()
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
        receipt_count = connection.execute("SELECT COUNT(*) FROM bootstrap_receipts").fetchone()
        consumed_limit = connection.execute(
            "SELECT COALESCE(SUM(count), 0) FROM rate_limits WHERE count > 0"
        ).fetchone()

    assert organization_count is not None
    assert session_count is not None
    assert receipt_count is not None
    assert consumed_limit is not None
    return (
        int(organization_count[0]),
        int(session_count[0]),
        int(receipt_count[0]),
        int(consumed_limit[0]),
    )


@pytest.mark.parametrize("key", [None, "", "   ", "x" * 129])
def test_bootstrap_requires_a_valid_idempotency_key_after_origin_validation(
    client: TestClient,
    key: str | None,
) -> None:
    headers = {"Origin": SAME_ORIGIN}
    if key is not None:
        headers["Idempotency-Key"] = key

    response = client.post(
        "/api/demo/workspaces",
        headers=headers,
        json={"initial_role": "manager"},
    )

    assert response.status_code == 400


def test_rejected_origin_takes_priority_over_a_missing_idempotency_key(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/demo/workspaces",
        headers={"Origin": "https://attacker.invalid"},
        json={"initial_role": "manager"},
    )

    assert response.status_code == 403


def test_serial_bootstrap_retry_replays_exactly_and_consumes_limits_once(
    app_harness: AppHarness,
) -> None:
    with app_harness.client(source_ip="198.51.100.90") as client:
        first = _bootstrap(client, key="serial-bootstrap-retry")
        replay = _bootstrap(client, key="serial-bootstrap-retry")

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.cookies["commerce_ops_session"] == first.cookies["commerce_ops_session"]
    assert _creation_counts(app_harness.database_path) == (1, 1, 1, 1)


def test_bootstrap_key_rejects_a_changed_payload_without_new_records(
    app_harness: AppHarness,
) -> None:
    with app_harness.client(source_ip="198.51.100.91") as client:
        first = _bootstrap(client, key="bootstrap-payload-conflict", role="manager")
        conflict = _bootstrap(client, key="bootstrap-payload-conflict", role="agent")

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert _creation_counts(app_harness.database_path) == (1, 1, 1, 1)


def test_same_bootstrap_key_is_independent_for_different_sources(
    app_harness: AppHarness,
) -> None:
    with app_harness.client(source_ip="198.51.100.92") as first_client:
        first = _bootstrap(first_client, key="source-scoped-bootstrap-key")
    with app_harness.client(source_ip="198.51.100.93") as second_client:
        second = _bootstrap(second_client, key="source-scoped-bootstrap-key")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["workspace"]["id"] != second.json()["workspace"]["id"]
    assert _creation_counts(app_harness.database_path) == (2, 2, 2, 2)


def test_bootstrap_retry_replays_after_an_app_restart(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    first_process = app_harness_factory()
    with first_process.client(source_ip="198.51.100.94") as first_client:
        first = _bootstrap(first_client, key="restart-safe-bootstrap")
    assert first.status_code == 201

    restarted_process = app_harness_factory(
        database_path=first_process.database_path,
        session_secret=first_process.settings.session_secret,
    )
    with restarted_process.client(source_ip="198.51.100.94") as replay_client:
        replay = _bootstrap(replay_client, key="restart-safe-bootstrap")

    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.cookies["commerce_ops_session"] == first.cookies["commerce_ops_session"]
    assert _creation_counts(first_process.database_path) == (1, 1, 1, 1)


def test_concurrent_bootstrap_retry_creates_and_counts_once(
    app_harness: AppHarness,
) -> None:
    start = Barrier(2)

    def send(request_number: int) -> Any:
        with app_harness.client(source_ip="198.51.100.95") as threaded_client:
            start.wait(timeout=10)
            return _bootstrap(
                threaded_client,
                key="concurrent-bootstrap-retry",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert (
        responses[0].cookies["commerce_ops_session"] == responses[1].cookies["commerce_ops_session"]
    )
    assert _creation_counts(app_harness.database_path) == (1, 1, 1, 1)


def test_bootstrap_receipt_never_stores_browser_or_source_secrets(
    app_harness: AppHarness,
) -> None:
    source_ip = "198.51.100.96"
    with app_harness.client(source_ip=source_ip) as client:
        response = _bootstrap(client, key="safe-bootstrap-receipt")
    assert response.status_code == 201

    raw_cookie = response.cookies["commerce_ops_session"]
    csrf_token = response.json()["csrf_token"]
    with sqlite3.connect(app_harness.database_path) as connection:
        row = connection.execute(
            """
            SELECT source_digest, payload_digest, response_json
            FROM bootstrap_receipts
            WHERE idempotency_key = 'safe-bootstrap-receipt'
            """
        ).fetchone()

    assert row is not None
    stored_values = " ".join(str(value) for value in row)
    assert source_ip not in stored_values
    assert raw_cookie not in stored_values
    assert csrf_token not in stored_values
