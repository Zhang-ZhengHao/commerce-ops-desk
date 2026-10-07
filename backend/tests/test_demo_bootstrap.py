"""Public demo bootstrap and session-recovery contracts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient


def test_demo_routes_are_not_registered_when_demo_mode_is_disabled(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(demo_mode=False)

    with harness.client() as disabled_client:
        bootstrap = disabled_client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN},
            json={"initial_role": "manager"},
        )
        session = disabled_client.get("/api/session")
        role = disabled_client.post(
            "/api/demo/role",
            headers={"Origin": SAME_ORIGIN},
            json={"role": "agent"},
        )

    assert bootstrap.status_code == 404
    assert session.status_code == 404
    assert role.status_code == 404


def test_public_demo_environment_marks_the_session_cookie_secure(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(environment="demo")

    with harness.client() as demo_client:
        response = demo_client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "secure-cookie-bootstrap",
            },
            json={"initial_role": "manager"},
        )

    assert response.status_code == 201
    assert "secure" in response.headers["set-cookie"].lower()


def test_bootstrap_rejects_missing_or_cross_origin_requests(
    client: TestClient,
) -> None:
    missing_origin = client.post(
        "/api/demo/workspaces",
        json={"initial_role": "manager"},
    )
    cross_origin = client.post(
        "/api/demo/workspaces",
        headers={"Origin": "https://attacker.invalid"},
        json={"initial_role": "manager"},
    )

    assert missing_origin.status_code == 403
    assert cross_origin.status_code == 403
    assert "commerce_ops_session" not in missing_origin.cookies
    assert "commerce_ops_session" not in cross_origin.cookies


def test_bootstrap_accepts_only_the_two_demo_roles(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    invalid_role = bootstrap_workspace(client, role="owner")
    incorrectly_cased_role = bootstrap_workspace(client, role="Manager")

    assert invalid_role.status_code == 422
    assert incorrectly_cased_role.status_code == 422
    assert "commerce_ops_session" not in invalid_role.cookies
    assert "commerce_ops_session" not in incorrectly_cased_role.cookies


def test_manager_bootstrap_sets_a_strict_http_only_session_and_returns_public_context(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    response = bootstrap_workspace(client, role="manager")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "workspace",
        "identity",
        "available_roles",
        "csrf_token",
    }
    assert set(body["workspace"]) == {"id", "name", "expires_at"}
    assert set(body["identity"]) == {
        "user_id",
        "membership_id",
        "display_name",
        "role",
    }
    assert body["identity"]["role"] == "manager"
    assert set(body["available_roles"]) == {"manager", "agent"}
    expires_at = body["workspace"]["expires_at"].replace("Z", "+00:00")
    assert expires_at == (app_harness.clock() + timedelta(hours=4)).isoformat()

    set_cookie = response.headers["set-cookie"].lower()
    assert "commerce_ops_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    raw_session = response.cookies["commerce_ops_session"]
    assert raw_session
    assert raw_session not in app_harness.token_factory.issued
    assert body["csrf_token"]
    assert body["csrf_token"] != raw_session
    assert raw_session not in body["csrf_token"]


def test_get_session_restores_the_same_workspace_identity_and_csrf_token(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    restored = client.get("/api/session")

    assert restored.status_code == 200
    assert restored.json() == bootstrap.json()


def test_get_session_survives_an_app_restart_with_the_same_database_and_secret(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    first_process = app_harness_factory()
    with first_process.client() as first_client:
        bootstrap = first_client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "session-restart-bootstrap",
            },
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201
        original_cookie = bootstrap.cookies["commerce_ops_session"]
        original_body = bootstrap.json()

    restarted_process = app_harness_factory(
        database_path=first_process.database_path,
        session_secret=first_process.settings.session_secret,
    )
    with restarted_process.client() as restarted_client:
        restarted_client.cookies.set("commerce_ops_session", original_cookie)
        restored = restarted_client.get("/api/session")

    assert restored.status_code == 200
    assert restored.json() == original_body


def test_get_session_without_a_valid_cookie_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/session")

    assert response.status_code == 401


def test_session_cannot_outlive_its_demo_workspace(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    app_harness.clock.advance(hours=4, seconds=1)

    assert client.get("/api/session").status_code == 401


def test_agent_entry_never_grants_that_identity_a_manager_membership(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    response = bootstrap_workspace(client, role="agent")

    assert response.status_code == 201
    body = response.json()
    assert body["identity"]["role"] == "agent"

    with sqlite3.connect(app_harness.database_path) as connection:
        roles = connection.execute(
            "SELECT role FROM memberships WHERE user_id = ? ORDER BY role",
            (body["identity"]["user_id"],),
        ).fetchall()

    assert roles == [("agent",)]


def test_role_switch_rejects_a_session_for_a_non_demo_organization(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    with sqlite3.connect(app_harness.database_path) as connection:
        connection.execute(
            "UPDATE organizations SET is_demo = 0 WHERE id = ?",
            (bootstrap.json()["workspace"]["id"],),
        )

    response = client.post(
        "/api/demo/role",
        headers={
            "Origin": SAME_ORIGIN,
            "X-CSRF-Token": bootstrap.json()["csrf_token"],
            "Idempotency-Key": "non-demo-workspace-must-not-switch",
        },
        json={"role": "agent"},
    )

    assert response.status_code == 404


def test_session_cookie_contains_no_plaintext_server_secret(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    response = bootstrap_workspace(client, role="manager")

    assert response.status_code == 201
    cookie_value = response.cookies["commerce_ops_session"]
    assert cookie_value
    assert "i02-test-only-session-secret" not in cookie_value
    assert SAME_ORIGIN not in cookie_value

    with sqlite3.connect(app_harness.database_path) as connection:
        stored_hashes = connection.execute("SELECT token_hash FROM sessions").fetchall()

    assert stored_hashes
    assert all(cookie_value not in stored_hash for (stored_hash,) in stored_hashes)
