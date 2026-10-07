"""Role-scoped case queue and workflow endpoints."""

from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.orm import Session

from app.auth.csrf import require_csrf, require_same_origin
from app.auth.dependencies import get_current_auth, get_database
from app.auth.permissions import require_role
from app.auth.session import AuthContext
from app.repositories.cases import CaseSeverity, CaseSort, CaseStatus
from app.services.case_commands import (
    AssigneeNotFoundError,
    CaseNotFoundError,
    CaseTransitionConflictError,
    CaseVersionConflictError,
    DemoCaseNoteLimitExceeded,
    ResolutionReasonInvalidError,
    add_case_note,
    assign_case,
    resolve_case,
)
from app.services.case_queries import (
    assignable_agents_payload,
    case_detail_payload,
    case_queue_payload,
)
from app.services.idempotent_commands import IdempotencyConflictError, valid_idempotency_key

router = APIRouter(prefix="/api", tags=["cases"])


class OrderSummaryPayload(BaseModel):
    id: str
    order_number: str
    amount_minor: int
    currency: str
    payment_status: str
    fulfillment_status: str


class AssigneePayload(BaseModel):
    membership_id: str
    display_name: str


class CaseSummaryPayload(BaseModel):
    id: str
    rule_key: str
    case_type: str
    severity: str
    status: str
    due_at: datetime
    updated_at: datetime
    version: int
    resolution_reason: str | None
    resolved_at: datetime | None
    order: OrderSummaryPayload
    assignee: AssigneePayload | None


class CaseListPayload(BaseModel):
    items: list[CaseSummaryPayload]
    total: int
    page: int
    page_size: int


class AgentListPayload(BaseModel):
    items: list[AssigneePayload]


class CaseNotePayload(BaseModel):
    id: str
    body: str
    author: AssigneePayload
    created_at: datetime


class AuditEventPayload(BaseModel):
    id: str
    action: str
    object_type: str
    object_id: str
    actor: AssigneePayload | None
    changes: dict[str, object]
    created_at: datetime


class CaseDetailPayload(CaseSummaryPayload):
    created_at: datetime
    resolution_reasons: list[str]
    notes: list[CaseNotePayload]
    audit_events: list[AuditEventPayload]


class CaseCommandPayload(BaseModel):
    case_id: str
    version: int


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    version: int = Field(ge=1)


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    version: int = Field(ge=1)


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _command_key(raw_key: str | None) -> str:
    key = valid_idempotency_key(raw_key)
    if key is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="A valid Idempotency-Key is required",
        )
    return key


@router.get("/agents", response_model=AgentListPayload)
def get_assignable_agents(
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
) -> AgentListPayload:
    require_role(context, "manager")
    return AgentListPayload.model_validate(
        assignable_agents_payload(database, organization_id=context.organization.id)
    )


@router.get("/cases", response_model=CaseListPayload)
def get_cases(
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
    status: Annotated[CaseStatus | None, Query()] = None,
    severity: Annotated[CaseSeverity | None, Query()] = None,
    rule_key: Annotated[str | None, Query(min_length=1, max_length=48)] = None,
    assignee_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[CaseSort, Query()] = "due_at",
) -> CaseListPayload:
    return CaseListPayload.model_validate(
        case_queue_payload(
            database,
            context=context,
            status=status,
            severity=severity,
            rule_key=rule_key,
            assignee_id=assignee_id,
            page=page,
            page_size=page_size,
            sort=sort,
        )
    )


@router.get("/cases/{case_id}", response_model=CaseDetailPayload)
def get_case_detail(
    case_id: str,
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
) -> CaseDetailPayload:
    payload = case_detail_payload(database, context=context, case_id=case_id)
    if payload is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        )
    return CaseDetailPayload.model_validate(payload)


@router.post("/cases/{case_id}/assignment", response_model=CaseCommandPayload)
def post_case_assignment(
    case_id: str,
    payload: AssignmentRequest,
    request: Request,
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
    idempotency_key_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> CaseCommandPayload:
    require_role(context, "manager")
    require_same_origin(request)
    settings = request.app.state.settings
    require_csrf(request, context, settings)
    key = _command_key(idempotency_key_header)
    clock = cast(Clock, request.app.state.clock)
    try:
        result = assign_case(
            database,
            context=context,
            case_id=case_id,
            assignee_id=payload.assignee_id,
            expected_version=payload.version,
            idempotency_key=key,
            now=clock(),
        )
    except (CaseNotFoundError, AssigneeNotFoundError):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None
    except CaseVersionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Case version conflict",
        ) from None
    except CaseTransitionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Case transition is not allowed",
        ) from None
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Idempotency key is already bound to another payload",
        ) from None
    return CaseCommandPayload.model_validate(result)


@router.post("/cases/{case_id}/notes", response_model=CaseCommandPayload)
def post_case_note(
    case_id: str,
    payload: NoteRequest,
    request: Request,
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
    idempotency_key_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> CaseCommandPayload:
    require_same_origin(request)
    settings = request.app.state.settings
    require_csrf(request, context, settings)
    key = _command_key(idempotency_key_header)
    clock = cast(Clock, request.app.state.clock)
    try:
        result = add_case_note(
            database,
            context=context,
            case_id=case_id,
            body=payload.body,
            expected_version=payload.version,
            idempotency_key=key,
            note_limit=settings.demo_case_note_limit,
            now=clock(),
        )
    except CaseNotFoundError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None
    except CaseVersionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Case version conflict",
        ) from None
    except CaseTransitionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Case transition is not allowed",
        ) from None
    except DemoCaseNoteLimitExceeded:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demo workspace case note limit exceeded",
        ) from None
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Idempotency key is already bound to another payload",
        ) from None
    return CaseCommandPayload.model_validate(result)


@router.post("/cases/{case_id}/resolution", response_model=CaseCommandPayload)
def post_case_resolution(
    case_id: str,
    payload: ResolutionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
    idempotency_key_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> CaseCommandPayload:
    require_same_origin(request)
    settings = request.app.state.settings
    require_csrf(request, context, settings)
    key = _command_key(idempotency_key_header)
    clock = cast(Clock, request.app.state.clock)
    try:
        result = resolve_case(
            database,
            context=context,
            case_id=case_id,
            reason=payload.reason,
            expected_version=payload.version,
            idempotency_key=key,
            now=clock(),
        )
    except CaseNotFoundError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None
    except CaseVersionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Case version conflict",
        ) from None
    except CaseTransitionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Case transition is not allowed",
        ) from None
    except ResolutionReasonInvalidError:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Resolution reason is not allowed for this case",
        ) from None
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Idempotency key is already bound to another payload",
        ) from None
    return CaseCommandPayload.model_validate(result)
