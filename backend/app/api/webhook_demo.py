"""Manager-only envelope creation for the synthetic provider demo."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy.orm import Session

from app.auth.csrf import require_csrf, require_same_origin
from app.auth.dependencies import get_current_auth, get_database
from app.auth.permissions import require_role
from app.auth.session import AuthContext
from app.config import Settings
from app.request_guard import VALIDATION_FAILED_RESPONSE
from app.services.webhook_simulator import (
    WebhookSimulatorUnavailable,
    build_signed_webhook_envelope,
)

router = APIRouter(prefix="/api/demo", tags=["demo"])


class WebhookEnvelopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    scenario: Literal["fresh", "stale"]


class WebhookEnvelopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Annotated[
        str,
        StringConstraints(
            pattern=(
                r"^/api/webhooks/synthetic/"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            )
        ),
    ]
    body: str
    timestamp: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,9}$")]
    event_id: Annotated[
        str,
        StringConstraints(pattern=r"^evt_[A-Za-z0-9]{8,64}$"),
    ]
    signature: Annotated[
        str,
        StringConstraints(pattern=r"^v1=[0-9a-f]{64}$"),
    ]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _require_manager_simulator_access(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_auth)],
) -> AuthContext:
    require_role(context, "manager")
    require_same_origin(request)
    settings = cast(Settings, request.app.state.settings)
    require_csrf(request, context, settings)
    return context


async def _parse_envelope_request(request: Request) -> WebhookEnvelopeRequest:
    try:
        return WebhookEnvelopeRequest.model_validate_json(await request.body())
    except (ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=VALIDATION_FAILED_RESPONSE["detail"],
        ) from None


@router.post("/webhooks/envelope", response_model=WebhookEnvelopePayload)
def post_webhook_envelope(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(_require_manager_simulator_access)],
    payload: Annotated[WebhookEnvelopeRequest, Depends(_parse_envelope_request)],
    database: Annotated[Session, Depends(get_database)],
) -> WebhookEnvelopePayload:
    settings = cast(Settings, request.app.state.settings)
    clock = cast(Clock, request.app.state.clock)
    try:
        envelope = build_signed_webhook_envelope(
            database,
            organization_id=context.organization.id,
            settings=settings,
            now=clock(),
            scenario=payload.scenario,
        )
    except WebhookSimulatorUnavailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None

    response.headers["Cache-Control"] = "no-store"
    return WebhookEnvelopePayload(
        path=envelope.path,
        body=envelope.body,
        timestamp=envelope.timestamp,
        event_id=envelope.event_id,
        signature=envelope.signature,
    )
