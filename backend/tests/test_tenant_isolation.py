"""Cross-organization resources are indistinguishable from absent resources."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
from fastapi import HTTPException


def _permissions_module() -> Any:
    return import_module("app.auth.permissions")


def test_resource_in_the_authenticated_organization_is_visible() -> None:
    permissions = _permissions_module()

    result = permissions.ensure_same_organization("organization-a", "organization-a")

    assert result is None


@pytest.mark.parametrize("role", ["manager", "agent"])
def test_cross_organization_resource_is_not_found_for_every_role(role: str) -> None:
    permissions = _permissions_module()
    hidden_resource_id = f"other-tenant-case-for-{role}"

    with pytest.raises(HTTPException) as exc_info:
        permissions.ensure_same_organization("organization-a", hidden_resource_id)

    assert exc_info.value.status_code == 404
    assert hidden_resource_id not in str(exc_info.value.detail)
