"""Bounded API request bodies and non-reflective validation errors."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from conftest import SAME_ORIGIN, AppHarness

BODY_TOO_LARGE = {
    "detail": {
        "code": "request_body_too_large",
        "message": "Request body exceeds the allowed size.",
    }
}
VALIDATION_FAILED = {
    "detail": {
        "code": "request_validation_failed",
        "message": "Request validation failed.",
    }
}


def _oversized_json(marker: str) -> bytes:
    return f'{{"initial_role":"{marker}"}}'.encode()


def test_declared_oversized_api_write_is_rejected_before_validation_or_origin(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(api_max_request_body_bytes=64)
    marker = "declared-secret-" * 8

    with harness.client() as client:
        response = client.post(
            "/api/demo/workspaces",
            content=_oversized_json(marker),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == BODY_TOO_LARGE
    assert marker not in response.text
    assert len(response.content) < 160


def test_streamed_api_write_without_content_length_is_still_bounded(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(api_max_request_body_bytes=64)
    marker = "streamed-secret-" * 8

    def request_chunks() -> Iterator[bytes]:
        payload = _oversized_json(marker)
        yield payload[:20]
        yield payload[20:80]
        yield payload[80:]

    with harness.client() as client:
        response = client.post(
            "/api/demo/workspaces",
            content=request_chunks(),
            headers={"Content-Type": "application/json"},
        )

    assert response.request.headers.get("content-length") is None
    assert response.status_code == 413
    assert response.json() == BODY_TOO_LARGE
    assert marker not in response.text


def test_forged_small_content_length_cannot_bypass_stream_limit(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(api_max_request_body_bytes=64)
    marker = "forged-length-secret-" * 8

    def request_chunks() -> Iterator[bytes]:
        payload = _oversized_json(marker)
        yield payload[:20]
        yield payload[20:80]
        yield payload[80:]

    with harness.client() as client:
        response = client.post(
            "/api/demo/workspaces",
            content=request_chunks(),
            headers={
                "Content-Length": "1",
                "Content-Type": "application/json",
            },
        )

    assert response.request.headers["content-length"] == "1"
    assert response.status_code == 413
    assert response.json() == BODY_TOO_LARGE
    assert marker not in response.text


def test_request_validation_response_is_stable_and_does_not_reflect_input(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(api_max_request_body_bytes=16 * 1024)
    secret_role = "customer-private-role-value"
    secret_field = "customer-private-field-name"
    secret_value = "customer-private-field-value"

    with harness.client() as client:
        response = client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN},
            json={
                "initial_role": secret_role,
                secret_field: secret_value,
            },
        )

    assert response.status_code == 422
    assert response.json() == VALIDATION_FAILED
    assert secret_role not in response.text
    assert secret_field not in response.text
    assert secret_value not in response.text
    assert len(response.content) < 160


def test_small_valid_api_write_is_unchanged(
    app_harness_factory: Callable[..., AppHarness],
    bootstrap_workspace: Callable[..., Any],
) -> None:
    harness = app_harness_factory(api_max_request_body_bytes=64)

    with harness.client() as client:
        response = bootstrap_workspace(client, role="manager")

    assert response.status_code == 201
    assert response.json()["identity"]["role"] == "manager"
