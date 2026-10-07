"""Bounded pagination contracts for list endpoints."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import SAME_ORIGIN, AppHarness


@pytest.mark.parametrize("endpoint", ["/api/cases", "/api/audit-events"])
def test_maximum_page_is_valid_and_returns_an_empty_page(
    app_harness_factory: Callable[..., AppHarness],
    endpoint: str,
) -> None:
    harness = app_harness_factory()

    with harness.client() as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={"Origin": SAME_ORIGIN, "Idempotency-Key": f"page-max-{endpoint}"},
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201

        response = client.get(endpoint, params={"page": 10_000})

    assert response.status_code == 200
    assert response.json()["page"] == 10_000
    assert response.json()["items"] == []


@pytest.mark.parametrize("endpoint", ["/api/cases", "/api/audit-events"])
@pytest.mark.parametrize("invalid_page", [10_001, 2**63])
def test_page_above_the_explicit_bound_is_a_validation_error(
    app_harness_factory: Callable[..., AppHarness],
    endpoint: str,
    invalid_page: int,
) -> None:
    harness = app_harness_factory()

    with harness.client(raise_server_exceptions=False) as client:
        bootstrap = client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": f"page-overflow-{endpoint}-{invalid_page}",
            },
            json={"initial_role": "manager"},
        )
        assert bootstrap.status_code == 201

        response = client.get(endpoint, params={"page": invalid_page})

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "request_validation_failed",
            "message": "Request validation failed.",
        }
    }
