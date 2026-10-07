"""Role-scoped application audit history."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_auth, get_database
from app.auth.session import AuthContext
from app.services.audit_queries import audit_history_payload

router = APIRouter(prefix="/api", tags=["audit"])


class AuditActorPayload(BaseModel):
    membership_id: str
    display_name: str


class AuditEventPayload(BaseModel):
    id: str
    action: str
    object_type: str
    object_id: str
    actor: AuditActorPayload | None
    changes: dict[str, object]
    created_at: datetime


class AuditListPayload(BaseModel):
    items: list[AuditEventPayload]
    total: int
    page: int
    page_size: int


@router.get("/audit-events", response_model=AuditListPayload)
def get_audit_events(
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
    case_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AuditListPayload:
    payload = audit_history_payload(
        database,
        context=context,
        case_id=case_id,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    return AuditListPayload.model_validate(payload)
