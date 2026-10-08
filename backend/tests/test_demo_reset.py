"""Atomic public-demo workspace reset contracts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient


def _reset_headers(csrf_token: str, *, origin: str = SAME_ORIGIN) -> dict[str, str]:
    return {
        "Origin": origin,
        "X-CSRF-Token": csrf_token,
    }


@pytest.mark.parametrize("role", ["manager", "agent"])
def test_reset_replaces_the_workspace_and_credentials_but_preserves_the_role(
    app_harness_factory: Callable[..., AppHarness],
    role: str,
) -> None:
    harness = app_harness_factory(demo_active_workspace_limit=1)

    with harness.client() as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": f"reset-{role}-bootstrap",
            },
            json={"initial_role": role},
        )
        assert bootstrap.status_code == 201
        old_payload = bootstrap.json()
        old_session = bootstrap.cookies["commerce_ops_session"]
        with sqlite3.connect(harness.database_path) as connection:
            old_integration = connection.execute(
                """
                SELECT id FROM webhook_integrations
                WHERE organization_id = ?
                """,
                (old_payload["workspace"]["id"],),
            ).fetchone()

        reset = client.post(
            "/api/demo/reset",
            headers=_reset_headers(old_payload["csrf_token"]),
        )

        assert reset.status_code == 201
        new_payload = reset.json()
        new_session = reset.cookies["commerce_ops_session"]
        assert new_payload["identity"]["role"] == role
        assert set(new_payload["available_roles"]) == {"manager", "agent"}
        assert new_payload["workspace"]["id"] != old_payload["workspace"]["id"]
        assert new_payload["identity"]["user_id"] != old_payload["identity"]["user_id"]
        assert new_payload["identity"]["membership_id"] != old_payload["identity"]["membership_id"]
        assert new_session != old_session
        assert new_payload["csrf_token"] != old_payload["csrf_token"]
        assert new_payload["csrf_token"] != new_session

        restored = client.get("/api/session")
        assert restored.status_code == 200
        assert restored.json() == new_payload

    with harness.client() as old_session_client:
        old_session_client.cookies.set("commerce_ops_session", old_session)
        assert old_session_client.get("/api/session").status_code == 401

    with sqlite3.connect(harness.database_path) as connection:
        organization_ids = connection.execute("SELECT id FROM organizations ORDER BY id").fetchall()
        order_count = connection.execute(
            "SELECT COUNT(*) FROM orders WHERE organization_id = ?",
            (new_payload["workspace"]["id"],),
        ).fetchone()
        integrations = connection.execute(
            """
            SELECT id, organization_id FROM webhook_integrations
            ORDER BY organization_id
            """
        ).fetchall()
        webhook_event_count = connection.execute(
            "SELECT webhook_event_count FROM organizations WHERE id = ?",
            (new_payload["workspace"]["id"],),
        ).fetchone()

    assert organization_ids == [(new_payload["workspace"]["id"],)]
    assert order_count == (4,)
    assert old_integration is not None
    assert len(integrations) == 1
    assert integrations == [(integrations[0][0], new_payload["workspace"]["id"])]
    assert integrations[0][0] != old_integration[0]
    assert webhook_event_count == (0,)


def test_reset_leaves_another_tenant_completely_unchanged(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory()

    with harness.client(source_ip="198.51.100.10") as first_client:
        first = first_client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN, "Idempotency-Key": "reset-first-tenant"},
            json={"initial_role": "manager"},
        )
        assert first.status_code == 201

        with harness.client(source_ip="198.51.100.20") as second_client:
            second = second_client.post(
                "/api/demo/workspaces",
                headers={
                    "Origin": SAME_ORIGIN,
                    "Idempotency-Key": "reset-second-tenant",
                },
                json={"initial_role": "agent"},
            )
            assert second.status_code == 201
            second_payload = second.json()

            reset = first_client.post(
                "/api/demo/reset",
                headers=_reset_headers(first.json()["csrf_token"]),
            )

            assert reset.status_code == 201
            assert reset.json()["workspace"]["id"] != first.json()["workspace"]["id"]
            second_restored = second_client.get("/api/session")
            assert second_restored.status_code == 200
            assert second_restored.json() == second_payload

    with sqlite3.connect(harness.database_path) as connection:
        second_workspace_count = connection.execute(
            "SELECT COUNT(*) FROM organizations WHERE id = ?",
            (second_payload["workspace"]["id"],),
        ).fetchone()

    assert second_workspace_count == (1,)


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"X-CSRF-Token": "will-be-replaced"}, 403),
        ({"Origin": "https://attacker.invalid", "X-CSRF-Token": "will-be-replaced"}, 403),
        ({"Origin": SAME_ORIGIN}, 403),
    ],
)
def test_reset_requires_same_origin_and_csrf_without_mutating_the_workspace(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
    headers: dict[str, str],
    expected_status: int,
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    original = bootstrap.json()
    supplied_headers = {
        key: original["csrf_token"] if value == "will-be-replaced" else value
        for key, value in headers.items()
    }

    response = client.post("/api/demo/reset", headers=supplied_headers)

    assert response.status_code == expected_status
    restored = client.get("/api/session")
    assert restored.status_code == 200
    assert restored.json() == original


def test_reset_requires_an_authenticated_session(client: TestClient) -> None:
    response = client.post(
        "/api/demo/reset",
        headers={"Origin": SAME_ORIGIN, "X-CSRF-Token": "not-a-session-token"},
    )

    assert response.status_code == 401


def test_reset_hides_and_preserves_a_non_demo_workspace(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    original = bootstrap.json()

    with sqlite3.connect(app_harness.database_path) as connection:
        connection.execute(
            "UPDATE organizations SET is_demo = 0 WHERE id = ?",
            (original["workspace"]["id"],),
        )

    response = client.post(
        "/api/demo/reset",
        headers=_reset_headers(original["csrf_token"]),
    )

    assert response.status_code == 404
    restored = client.get("/api/session")
    assert restored.status_code == 200
    assert restored.json() == original


def test_reset_consumes_creation_quota_and_rolls_back_when_the_limit_is_reached(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_source_hourly_limit=2)

    with harness.client(source_ip="198.51.100.30") as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN, "Idempotency-Key": "reset-quota-bootstrap"},
            json={"initial_role": "agent"},
        )
        assert bootstrap.status_code == 201

        first_reset = client.post(
            "/api/demo/reset",
            headers=_reset_headers(bootstrap.json()["csrf_token"]),
        )
        assert first_reset.status_code == 201
        current_payload = first_reset.json()
        current_session = first_reset.cookies["commerce_ops_session"]

        rejected = client.post(
            "/api/demo/reset",
            headers=_reset_headers(current_payload["csrf_token"]),
        )

        assert rejected.status_code == 429
        assert rejected.headers["Retry-After"] == "3600"
        assert "commerce_ops_session" not in rejected.cookies
        restored = client.get("/api/session")
        assert restored.status_code == 200
        assert restored.json() == current_payload
        assert client.cookies["commerce_ops_session"] == current_session

    with sqlite3.connect(harness.database_path) as connection:
        organization_ids = connection.execute("SELECT id FROM organizations").fetchall()

    assert organization_ids == [(current_payload["workspace"]["id"],)]


def test_reset_rolls_back_the_deleted_workspace_when_creation_crashes(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory()

    with harness.client() as bootstrap_client:
        bootstrap = bootstrap_client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN, "Idempotency-Key": "reset-crash-bootstrap"},
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201
        original_payload = bootstrap.json()
        original_session = bootstrap.cookies["commerce_ops_session"]

    with sqlite3.connect(harness.database_path) as connection:
        original_integration = connection.execute(
            """
            SELECT id FROM webhook_integrations
            WHERE organization_id = ?
            """,
            (original_payload["workspace"]["id"],),
        ).fetchone()
        connection.execute(
            """
            UPDATE organizations SET webhook_event_count = 7
            WHERE id = ?
            """,
            (original_payload["workspace"]["id"],),
        )

    def fail_after_workspace_rows_are_staged() -> str:
        raise RuntimeError("synthetic reset creation failed")

    harness.app.state.token_factory = fail_after_workspace_rows_are_staged
    with harness.client(raise_server_exceptions=False) as reset_client:
        reset_client.cookies.set("commerce_ops_session", original_session)

        failed = reset_client.post(
            "/api/demo/reset",
            headers=_reset_headers(original_payload["csrf_token"]),
        )

        assert failed.status_code == 500
        assert "commerce_ops_session" not in failed.cookies
        restored = reset_client.get("/api/session")
        assert restored.status_code == 200
        assert restored.json() == original_payload

    with sqlite3.connect(harness.database_path) as connection:
        organizations = connection.execute(
            "SELECT id, webhook_event_count FROM organizations ORDER BY id"
        ).fetchall()
        integrations = connection.execute(
            "SELECT id, organization_id FROM webhook_integrations ORDER BY id"
        ).fetchall()

    assert original_integration is not None
    assert organizations == [(original_payload["workspace"]["id"], 7)]
    assert integrations == [(original_integration[0], original_payload["workspace"]["id"])]


def test_concurrent_reset_with_one_old_session_creates_one_replacement(
    app_harness_factory: Callable[..., AppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import demo as demo_module
    from app.services.demo_workspaces import GLOBAL_CAPACITY_LOCK_DIGEST

    harness = app_harness_factory()
    with harness.client(source_ip="198.51.100.44") as bootstrap_client:
        bootstrap = bootstrap_client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN, "Idempotency-Key": "reset-race-bootstrap"},
            json={"initial_role": "manager"},
        )
    assert bootstrap.status_code == 201
    original_workspace_id = bootstrap.json()["workspace"]["id"]
    original_cookie = bootstrap.cookies["commerce_ops_session"]
    original_csrf = bootstrap.json()["csrf_token"]

    demo_api: Any = demo_module
    original_require_csrf = demo_api.require_csrf
    ready_to_claim = Barrier(2)

    def synchronized_require_csrf(*args: Any, **kwargs: Any) -> None:
        original_require_csrf(*args, **kwargs)
        ready_to_claim.wait(timeout=10)

    monkeypatch.setattr(demo_api, "require_csrf", synchronized_require_csrf)

    def send_reset(_request_number: int) -> Any:
        with harness.client(
            source_ip="198.51.100.44",
            raise_server_exceptions=False,
        ) as threaded_client:
            threaded_client.cookies.set("commerce_ops_session", original_cookie)
            return threaded_client.post(
                "/api/demo/reset",
                headers=_reset_headers(original_csrf),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send_reset, range(2)))

    assert sorted(response.status_code for response in responses) == [201, 401]
    winner = next(response for response in responses if response.status_code == 201)
    assert winner.json()["workspace"]["id"] != original_workspace_id

    with sqlite3.connect(harness.database_path) as connection:
        organizations = connection.execute("SELECT id FROM organizations ORDER BY id").fetchall()
        source_counts = connection.execute(
            "SELECT count FROM rate_limits WHERE source_digest != ?",
            (GLOBAL_CAPACITY_LOCK_DIGEST,),
        ).fetchall()
        integrations = connection.execute(
            "SELECT organization_id FROM webhook_integrations"
        ).fetchall()

    assert organizations == [(winner.json()["workspace"]["id"],)]
    assert source_counts == [(2,)]
    assert integrations == [(winner.json()["workspace"]["id"],)]
