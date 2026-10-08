"""Public synthetic webhook ingress."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.auth.webhook import (
    WebhookAuthHeaders,
    derive_webhook_integration_key,
    is_webhook_timestamp_current,
    parse_canonical_webhook_path,
    parse_webhook_auth_headers,
    verify_webhook_signature,
)
from app.config import Settings
from app.domain.webhook_event import (
    InvalidWebhookPayload,
    UnsupportedWebhookMediaType,
    parse_synthetic_webhook_event,
)
from app.repositories.webhook_integrations import find_active_demo_webhook_target
from app.services.demo_workspaces import client_source_address
from app.services.webhook_ingress import (
    WebhookIngressRateLimitExceeded,
    enforce_webhook_source_limit,
)
from app.services.webhook_processing import (
    DemoWebhookEventLimitExceeded,
    WebhookEventDigestConflict,
    WebhookEventInvariantViolation,
    WebhookOrderSnapshotConflict,
    WebhookProcessingTargetUnavailable,
    process_payment_failed_webhook,
)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)

AUTHENTICATION_FAILED_DETAIL = {
    "code": "webhook_authentication_failed",
    "message": "Webhook authentication failed.",
}
MEDIA_TYPE_UNSUPPORTED_DETAIL = {
    "code": "webhook_media_type_unsupported",
    "message": "Webhook media type is unsupported.",
}
PAYLOAD_INVALID_DETAIL = {
    "code": "webhook_payload_invalid",
    "message": "Webhook payload is invalid.",
}
EVENT_CONFLICT_DETAIL = {
    "code": "webhook_event_conflict",
    "message": "Webhook event conflicts with stored data.",
}
SOURCE_RATE_LIMITED_DETAIL = {
    "code": "webhook_ingress_rate_limited",
    "message": "Webhook ingress rate limit exceeded.",
}
BUSINESS_LIMIT_REACHED_DETAIL = {
    "code": "demo_webhook_limit_reached",
    "message": "Demo webhook event limit reached.",
}
SERVICE_UNAVAILABLE_DETAIL = {
    "code": "webhook_service_unavailable",
    "message": "Webhook service is temporarily unavailable.",
}
DUMMY_INTEGRATION_ID = "00000000-0000-4000-8000-000000000000"
DUMMY_AUTH_HEADERS = WebhookAuthHeaders(
    timestamp=1,
    event_id="evt_00000000",
    signature=b"\x00" * 32,
)


class Clock(Protocol):
    def __call__(self) -> datetime: ...


async def _read_raw_body(request: Request) -> bytes:
    return await request.body()


def _error_response(
    status_code: int,
    detail: dict[str, str],
    *,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = None if retry_after is None else {"Retry-After": str(retry_after)}
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )


def _request_id() -> str:
    return uuid4().hex


def _service_unavailable(error: Exception, *, phase: str) -> JSONResponse:
    logger.error(
        "request_id=%s phase=%s code=webhook_service_unavailable exception=%s",
        _request_id(),
        phase,
        type(error).__name__,
    )
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        SERVICE_UNAVAILABLE_DETAIL,
        retry_after=1,
    )


@router.post("/synthetic/{integration_id:path}")
def receive_synthetic_webhook(
    integration_id: str,
    request: Request,
    raw_body: Annotated[bytes, Depends(_read_raw_body)],
) -> JSONResponse:
    settings = cast(Settings, request.app.state.settings)
    clock = cast(Clock, request.app.state.clock)
    session_factory = cast(sessionmaker[Session], request.app.state.session_factory)
    try:
        received_at = clock()
    except Exception as error:
        return _service_unavailable(error, phase="clock")

    try:
        enforce_webhook_source_limit(
            session_factory,
            settings=settings,
            normalized_source=client_source_address(request, settings),
            now=received_at,
        )
    except WebhookIngressRateLimitExceeded as error:
        return _error_response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            SOURCE_RATE_LIMITED_DETAIL,
            retry_after=error.retry_after,
        )
    except Exception as error:
        return _service_unavailable(error, phase="source_limit")

    try:
        raw_path_value = request.scope.get("raw_path")
        raw_path = raw_path_value if isinstance(raw_path_value, bytes) else None
        raw_headers = cast(Sequence[tuple[bytes, bytes]], request.scope["headers"])
        parsed_integration_id = parse_canonical_webhook_path(
            route_integration_id=integration_id,
            raw_path=raw_path,
        )
        parsed_headers = parse_webhook_auth_headers(raw_headers)

        target = None
        if parsed_integration_id is not None:
            with session_factory() as target_database:
                target = find_active_demo_webhook_target(
                    target_database,
                    integration_id=parsed_integration_id,
                    now=received_at,
                )

        secret = settings.webhook_master_secret
        if secret is None:  # The route exists only for validated enabled settings.
            raise RuntimeError("webhook ingress configuration is unavailable")
        master_secret = secret.get_secret_value().encode("utf-8")
        signing_integration_id = parsed_integration_id or DUMMY_INTEGRATION_ID
        integration_key = derive_webhook_integration_key(
            master_secret,
            integration_id=(target.integration_id if target is not None else DUMMY_INTEGRATION_ID),
            key_version=(target.key_version if target is not None else 1),
        )
        authentication_headers = parsed_headers or DUMMY_AUTH_HEADERS
        signature_valid = verify_webhook_signature(
            provided_signature=authentication_headers.signature,
            integration_key=integration_key,
            timestamp=authentication_headers.timestamp,
            integration_id=signing_integration_id,
            event_id=authentication_headers.event_id,
            raw_body=raw_body,
        )
        timestamp_is_current = is_webhook_timestamp_current(
            timestamp=authentication_headers.timestamp,
            now=received_at,
        )
    except Exception as error:
        return _service_unavailable(error, phase="authentication")

    if (
        parsed_integration_id is None
        or parsed_headers is None
        or target is None
        or not signature_valid
        or not timestamp_is_current
    ):
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            AUTHENTICATION_FAILED_DETAIL,
        )

    try:
        payload = parse_synthetic_webhook_event(
            raw_body=raw_body,
            raw_headers=raw_headers,
        )
    except UnsupportedWebhookMediaType:
        return _error_response(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            MEDIA_TYPE_UNSUPPORTED_DETAIL,
        )
    except InvalidWebhookPayload:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            PAYLOAD_INVALID_DETAIL,
        )
    except Exception as error:
        return _service_unavailable(error, phase="payload")

    payload_digest = hashlib.sha256(raw_body).hexdigest()
    try:
        with session_factory() as business_database:
            result = process_payment_failed_webhook(
                business_database,
                organization_id=target.organization_id,
                integration_id=target.integration_id,
                integration_key_version=target.key_version,
                external_event_id=parsed_headers.event_id,
                payload_digest=payload_digest,
                payload=payload,
                event_limit=settings.demo_webhook_event_limit,
                received_at=received_at,
            )
    except WebhookProcessingTargetUnavailable:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            AUTHENTICATION_FAILED_DETAIL,
        )
    except (WebhookEventDigestConflict, WebhookOrderSnapshotConflict):
        return _error_response(
            status.HTTP_409_CONFLICT,
            EVENT_CONFLICT_DETAIL,
        )
    except DemoWebhookEventLimitExceeded:
        return _error_response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            BUSINESS_LIMIT_REACHED_DETAIL,
        )
    except WebhookEventInvariantViolation as error:
        return _service_unavailable(error, phase="processing")
    except Exception as error:
        return _service_unavailable(error, phase="processing")

    return JSONResponse(
        status_code=(status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED),
        content={
            "status": "processed",
            "event_id": result.event_id,
            "case_id": result.case_id,
            "replayed": result.replayed,
        },
    )
