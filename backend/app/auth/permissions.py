"""Reusable role and tenant-visibility guards."""

from fastapi import HTTPException, status

from app.auth.session import AuthContext
from app.models.membership import Role


def require_role(context: AuthContext, *allowed_roles: Role) -> AuthContext:
    if context.membership.role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
    return context


def ensure_same_organization(
    current_organization_id: str,
    resource_organization_id: str,
) -> None:
    if current_organization_id != resource_organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
