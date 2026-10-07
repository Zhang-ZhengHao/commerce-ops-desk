"""Role-scoped operational dashboard."""

from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_auth, get_database
from app.auth.session import AuthContext
from app.services.case_queries import dashboard_payload

router = APIRouter(prefix="/api", tags=["operations"])


class DashboardSummary(BaseModel):
    open: int
    approaching_sla: int
    high_severity: int
    resolved: int


class RuleCount(BaseModel):
    rule_key: str
    case_type: str
    count: int


class DashboardPayload(BaseModel):
    generated_at: datetime
    summary: DashboardSummary
    by_rule: list[RuleCount]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


@router.get("/dashboard", response_model=DashboardPayload)
def get_dashboard(
    request: Request,
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
) -> DashboardPayload:
    clock = cast(Clock, request.app.state.clock)
    return DashboardPayload.model_validate(
        dashboard_payload(database, context=context, now=clock())
    )
