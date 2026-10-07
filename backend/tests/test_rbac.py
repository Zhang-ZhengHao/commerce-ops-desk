"""Role checks remain a server-side boundary independent of the UI."""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException


def _permissions_module() -> Any:
    return import_module("app.auth.permissions")


def _context(role: str) -> SimpleNamespace:
    return SimpleNamespace(membership=SimpleNamespace(role=role))


@pytest.mark.parametrize(
    ("role", "allowed_roles"),
    [
        ("manager", ("manager",)),
        ("manager", ("manager", "agent")),
        ("agent", ("agent",)),
        ("agent", ("manager", "agent")),
    ],
)
def test_require_role_returns_the_authenticated_context_when_allowed(
    role: str,
    allowed_roles: tuple[str, ...],
) -> None:
    permissions = _permissions_module()
    context = _context(role)

    result = permissions.require_role(context, *allowed_roles)

    assert result is context


def test_agent_cannot_cross_a_manager_only_boundary() -> None:
    permissions = _permissions_module()

    with pytest.raises(HTTPException) as exc_info:
        permissions.require_role(_context("agent"), "manager")

    assert exc_info.value.status_code == 403


def test_role_failure_does_not_disclose_membership_details() -> None:
    permissions = _permissions_module()
    membership_id = "membership-that-must-not-leak"
    context = SimpleNamespace(membership=SimpleNamespace(id=membership_id, role="agent"))

    with pytest.raises(HTTPException) as exc_info:
        permissions.require_role(context, "manager")

    assert membership_id not in str(exc_info.value.detail)
