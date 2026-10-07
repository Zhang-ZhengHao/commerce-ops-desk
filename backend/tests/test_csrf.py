"""Cookie-session CSRF and credential-rotation contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from conftest import SAME_ORIGIN, AppHarness
from fastapi.testclient import TestClient


def _role_headers(csrf_token: str, *, origin: str = SAME_ORIGIN) -> dict[str, str]:
    return {
        "Origin": origin,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": "role-switch-csrf-contract",
    }


def test_cookie_authenticated_write_rejects_a_missing_csrf_token(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    response = client.post(
        "/api/demo/role",
        headers={
            "Origin": SAME_ORIGIN,
            "Idempotency-Key": "role-switch-without-csrf",
        },
        json={"role": "agent"},
    )

    assert response.status_code == 403


def test_cookie_authenticated_write_rejects_a_cross_origin_request(
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201

    response = client.post(
        "/api/demo/role",
        headers=_role_headers(
            bootstrap.json()["csrf_token"],
            origin="https://attacker.invalid",
        ),
        json={"role": "agent"},
    )

    assert response.status_code == 403


def test_role_switch_rotates_session_and_csrf_and_invalidates_old_credentials(
    app_harness: AppHarness,
    client: TestClient,
    bootstrap_workspace: Callable[..., Any],
) -> None:
    bootstrap = bootstrap_workspace(client, role="manager")
    assert bootstrap.status_code == 201
    old_csrf = bootstrap.json()["csrf_token"]
    old_session = bootstrap.cookies["commerce_ops_session"]

    switched = client.post(
        "/api/demo/role",
        headers=_role_headers(old_csrf),
        json={"role": "agent"},
    )

    assert switched.status_code == 200
    switched_body = switched.json()
    assert switched_body["identity"]["role"] == "agent"
    assert switched_body["workspace"] == bootstrap.json()["workspace"]
    assert switched_body["csrf_token"] != old_csrf
    new_session = switched.cookies["commerce_ops_session"]
    assert new_session != old_session
    assert len(new_session) >= 43
    assert switched_body["csrf_token"] != new_session
    assert new_session not in switched_body["csrf_token"]

    with app_harness.client() as old_credentials_client:
        old_credentials_client.cookies.set("commerce_ops_session", old_session)
        old_session_read = old_credentials_client.get("/api/session")
        old_session_write = old_credentials_client.post(
            "/api/demo/role",
            headers={
                **_role_headers(old_csrf),
                "Idempotency-Key": "a-different-command",
            },
            json={"role": "manager"},
        )

    assert old_session_read.status_code == 401
    assert old_session_write.status_code == 401

    restored = client.get("/api/session")
    assert restored.status_code == 200
    assert restored.json() == switched_body
