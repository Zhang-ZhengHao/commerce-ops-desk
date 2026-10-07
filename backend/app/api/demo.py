"""Public-demo bootstrap, session recovery, and role rotation endpoints."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.csrf import derive_csrf_token, require_csrf, require_same_origin
from app.auth.dependencies import get_current_auth, get_database
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    AuthContext,
    as_utc,
    create_session,
    derive_bootstrap_session_token,
    derive_rotated_session_token,
    keyed_digest,
    load_auth_context,
)
from app.config import Settings
from app.models import CommandReceipt, Membership, Role
from app.services.bootstrap_idempotency import (
    claim_bootstrap_receipt,
    complete_bootstrap_receipt,
    find_bootstrap_receipt,
    replay_bootstrap_receipt,
)
from app.services.demo_workspaces import (
    DemoCapacityExceeded,
    DemoRateLimitExceeded,
    DemoRoleWriteLimitExceeded,
    claim_session_rotation,
    client_source_address,
    create_demo_workspace,
    enforce_role_write_limit,
    find_membership_for_role,
    link_session_replacement,
    lock_workspace_role_writes,
)
from app.services.idempotent_commands import (
    IdempotencyConflictError,
    canonical_json_digest,
    find_command_receipt,
    find_command_receipt_by_result_session,
    replay_command,
    store_command_receipt,
)

router = APIRouter(prefix="/api", tags=["demo"])
__all__ = ["find_command_receipt", "router"]
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[!-~]{1,128}$")
ROLE_SWITCH_COMMAND = "demo.role.switch"


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_role: Role


class RoleSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


class WorkspacePayload(BaseModel):
    id: str
    name: str
    expires_at: datetime


class IdentityPayload(BaseModel):
    user_id: str
    membership_id: str
    display_name: str
    role: Role


class SessionPayload(BaseModel):
    workspace: WorkspacePayload
    identity: IdentityPayload
    available_roles: list[Role]
    csrf_token: str


def _clock(request: Request) -> datetime:
    return as_utc(cast("Clock", request.app.state.clock)())


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _available_roles(db: Session, organization_id: str) -> list[Role]:
    persisted_roles = set(
        db.scalars(
            select(Membership.role).where(Membership.organization_id == organization_id)
        ).all()
    )
    ordered_roles: tuple[Role, ...] = ("manager", "agent")
    return [role for role in ordered_roles if role in persisted_roles]


def _session_payload(
    *,
    context: AuthContext,
    available_roles: list[Role],
    csrf_token: str,
) -> SessionPayload:
    return SessionPayload(
        workspace=WorkspacePayload(
            id=context.organization.id,
            name=context.organization.name,
            expires_at=as_utc(context.organization.expires_at),
        ),
        identity=IdentityPayload(
            user_id=context.user.id,
            membership_id=context.membership.id,
            display_name=context.user.display_name,
            role=cast(Role, context.membership.role),
        ),
        available_roles=available_roles,
        csrf_token=csrf_token,
    )


def _set_session_cookie(
    response: Response,
    *,
    settings: Settings,
    raw_token: str,
    expires_at: datetime,
    now: datetime,
) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max(0, int((as_utc(expires_at) - as_utc(now)).total_seconds())),
        expires=as_utc(expires_at),
        path=SESSION_COOKIE_PATH,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )


def _replay_workspace_bootstrap(
    database: Session,
    *,
    settings: Settings,
    now: datetime,
    source_digest: str,
    idempotency_key: str,
    payload_digest: str,
    raw_token: str,
) -> tuple[SessionPayload, AuthContext]:
    receipt = find_bootstrap_receipt(
        database,
        source_digest=source_digest,
        idempotency_key=idempotency_key,
    )
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored command result is no longer available",
        )
    try:
        stored_result = replay_bootstrap_receipt(
            receipt,
            payload_digest=payload_digest,
        )
    except IdempotencyConflictError:
        raise _idempotency_conflict() from None

    context = load_auth_context(
        database,
        raw_token=raw_token,
        settings=settings,
        now=now,
    )
    if (
        context is None
        or context.organization.id != stored_result.organization_id
        or context.session.id != stored_result.result_session_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored command result is no longer available",
        )

    response_body = dict(stored_result.response_body)
    response_body["csrf_token"] = derive_csrf_token(settings, raw_token)
    result = SessionPayload.model_validate(response_body)
    if (
        result.workspace.id != context.organization.id
        or result.identity.user_id != context.user.id
        or result.identity.membership_id != context.membership.id
        or result.identity.role != context.membership.role
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored command result is no longer available",
        )
    return result, context


@router.post(
    "/demo/workspaces",
    response_model=SessionPayload,
    status_code=status.HTTP_201_CREATED,
)
def create_workspace(
    payload: WorkspaceCreateRequest,
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_database)],
    idempotency_key_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> SessionPayload:
    require_same_origin(request)
    idempotency_key = _validated_idempotency_key(idempotency_key_header)
    settings = _settings(request)
    now = _clock(request)
    source_address = client_source_address(request, settings)
    source_digest = keyed_digest(settings, "rate-limit-source", source_address)
    payload_digest = canonical_json_digest({"initial_role": payload.initial_role})
    raw_session_token = derive_bootstrap_session_token(
        settings,
        source_digest=source_digest,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
    )
    claimed = claim_bootstrap_receipt(
        database,
        source_digest=source_digest,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        now=now,
    )
    if not claimed:
        result, context = _replay_workspace_bootstrap(
            database,
            settings=settings,
            now=now,
            source_digest=source_digest,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            raw_token=raw_session_token,
        )
        _set_session_cookie(
            response,
            settings=settings,
            raw_token=raw_session_token,
            expires_at=context.organization.expires_at,
            now=now,
        )
        return result

    try:
        created = create_demo_workspace(
            database,
            initial_role=payload.initial_role,
            source_address=source_address,
            settings=settings,
            now=now,
            token_factory=lambda: raw_session_token,
        )
    except DemoRateLimitExceeded as exc:
        database.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demo workspace rate limit reached",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except DemoCapacityExceeded:
        database.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo workspace capacity reached",
        ) from None

    context = AuthContext(
        session=created.session,
        membership=created.membership,
        user=created.user,
        organization=created.organization,
        raw_token=created.raw_session_token,
    )
    result = _session_payload(
        context=context,
        available_roles=list(created.available_roles),
        csrf_token=derive_csrf_token(settings, created.raw_session_token),
    )
    complete_bootstrap_receipt(
        database,
        source_digest=source_digest,
        idempotency_key=idempotency_key,
        organization_id=created.organization.id,
        result_session_id=created.session.id,
        response_body=result.model_dump(mode="json", exclude={"csrf_token"}),
    )
    database.commit()
    _set_session_cookie(
        response,
        settings=settings,
        raw_token=created.raw_session_token,
        expires_at=created.organization.expires_at,
        now=now,
    )
    return result


@router.get("/session", response_model=SessionPayload)
def get_session(
    database: Annotated[Session, Depends(get_database)],
    context: Annotated[AuthContext, Depends(get_current_auth)],
    request: Request,
    response: Response,
) -> SessionPayload:
    settings = _settings(request)
    response.headers["Cache-Control"] = "no-store"
    return _session_payload(
        context=context,
        available_roles=_available_roles(database, context.organization.id),
        csrf_token=derive_csrf_token(settings, context.raw_token),
    )


def _validated_idempotency_key(raw_key: str | None) -> str:
    if raw_key is None or IDEMPOTENCY_KEY_PATTERN.fullmatch(raw_key) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid Idempotency-Key is required",
        )
    return raw_key


def _authentication_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Idempotency key is already bound to another payload",
    )


def _workspace_retry_after(context: AuthContext, now: datetime) -> int:
    return max(
        1,
        math.ceil((as_utc(context.organization.expires_at) - as_utc(now)).total_seconds()),
    )


def _find_role_switch_receipt(
    database: Session,
    *,
    context: AuthContext,
    idempotency_key: str,
) -> CommandReceipt | None:
    result_receipt = find_command_receipt_by_result_session(
        database,
        organization_id=context.organization.id,
        result_session_id=context.session.id,
        command_type=ROLE_SWITCH_COMMAND,
        idempotency_key=idempotency_key,
    )
    if result_receipt is not None:
        return result_receipt
    return find_command_receipt(
        database,
        membership_id=context.membership.id,
        command_type=ROLE_SWITCH_COMMAND,
        idempotency_key=idempotency_key,
    )


def _replay_role_switch(
    database: Session,
    *,
    receipt: CommandReceipt,
    context: AuthContext,
    settings: Settings,
    now: datetime,
    idempotency_key: str,
    payload_digest: str,
) -> tuple[SessionPayload, str]:
    try:
        stored_result = replay_command(receipt, payload_digest=payload_digest)
    except IdempotencyConflictError:
        raise _idempotency_conflict() from None

    if context.session.id == stored_result.result_session_id:
        replacement_token = context.raw_token
    else:
        replacement_token = derive_rotated_session_token(
            settings,
            previous_token=context.raw_token,
            command_type=ROLE_SWITCH_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )
    replacement_context = load_auth_context(
        database,
        raw_token=replacement_token,
        settings=settings,
        now=now,
    )
    if (
        replacement_context is None
        or replacement_context.organization.id != context.organization.id
        or replacement_context.membership.id != stored_result.result_membership_id
        or replacement_context.session.id != stored_result.result_session_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored command result is no longer available",
        )

    response_body = dict(stored_result.response_body)
    response_body["csrf_token"] = derive_csrf_token(settings, replacement_token)
    return SessionPayload.model_validate(response_body), replacement_token


@router.post("/demo/role", response_model=SessionPayload)
def switch_role(
    payload: RoleSwitchRequest,
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_database)],
    idempotency_key_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> SessionPayload:
    require_same_origin(request)
    settings = _settings(request)
    now = _clock(request)
    idempotency_key = _validated_idempotency_key(idempotency_key_header)
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is None:
        raise _authentication_required()

    context = load_auth_context(
        database,
        raw_token=raw_token,
        settings=settings,
        now=now,
        allow_revoked=True,
    )
    if context is None:
        raise _authentication_required()
    if not context.organization.is_demo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        )
    require_csrf(request, context, settings)

    payload_digest = canonical_json_digest({"role": payload.role})
    receipt = _find_role_switch_receipt(
        database,
        context=context,
        idempotency_key=idempotency_key,
    )
    if receipt is not None:
        result, replacement_token = _replay_role_switch(
            database,
            receipt=receipt,
            context=context,
            settings=settings,
            now=now,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )
    else:
        if context.session.revoked_at is not None:
            raise _authentication_required()
        if context.membership.role == payload.role:
            replacement_token = context.raw_token
            result = _session_payload(
                context=context,
                available_roles=_available_roles(database, context.organization.id),
                csrf_token=derive_csrf_token(settings, replacement_token),
            )
        else:
            lock_workspace_role_writes(
                database,
                organization_id=context.organization.id,
            )
            receipt = _find_role_switch_receipt(
                database,
                context=context,
                idempotency_key=idempotency_key,
            )
            if receipt is not None:
                result, replacement_token = _replay_role_switch(
                    database,
                    receipt=receipt,
                    context=context,
                    settings=settings,
                    now=now,
                    idempotency_key=idempotency_key,
                    payload_digest=payload_digest,
                )
            else:
                try:
                    enforce_role_write_limit(
                        database,
                        organization_id=context.organization.id,
                        command_type=ROLE_SWITCH_COMMAND,
                        maximum=settings.demo_role_write_limit,
                    )
                except DemoRoleWriteLimitExceeded:
                    database.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Demo workspace role-write limit reached",
                        headers={"Retry-After": str(_workspace_retry_after(context, now))},
                    ) from None

                target = find_membership_for_role(
                    database,
                    organization_id=context.organization.id,
                    role=payload.role,
                )
                if target is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Resource not found",
                    )
                target_membership, target_user = target
                replacement_token = derive_rotated_session_token(
                    settings,
                    previous_token=context.raw_token,
                    command_type=ROLE_SWITCH_COMMAND,
                    idempotency_key=idempotency_key,
                    payload_digest=payload_digest,
                )

                if not claim_session_rotation(database, session=context.session, now=now):
                    database.rollback()
                    refreshed_context = load_auth_context(
                        database,
                        raw_token=raw_token,
                        settings=settings,
                        now=now,
                        allow_revoked=True,
                    )
                    if refreshed_context is None:
                        raise _authentication_required()
                    receipt = _find_role_switch_receipt(
                        database,
                        context=refreshed_context,
                        idempotency_key=idempotency_key,
                    )
                    if receipt is None:
                        raise _authentication_required()
                    result, replacement_token = _replay_role_switch(
                        database,
                        receipt=receipt,
                        context=refreshed_context,
                        settings=settings,
                        now=now,
                        idempotency_key=idempotency_key,
                        payload_digest=payload_digest,
                    )
                else:
                    replacement, replacement_token = create_session(
                        database,
                        organization_id=context.organization.id,
                        membership_id=target_membership.id,
                        expires_at=context.organization.expires_at,
                        now=now,
                        settings=settings,
                        token_factory=lambda: replacement_token,
                    )
                    link_session_replacement(
                        database,
                        session=context.session,
                        replacement=replacement,
                    )
                    replacement_context = AuthContext(
                        session=replacement,
                        membership=target_membership,
                        user=target_user,
                        organization=context.organization,
                        raw_token=replacement_token,
                    )
                    result = _session_payload(
                        context=replacement_context,
                        available_roles=_available_roles(database, context.organization.id),
                        csrf_token=derive_csrf_token(settings, replacement_token),
                    )
                    safe_result = result.model_dump(mode="json", exclude={"csrf_token"})
                    store_command_receipt(
                        database,
                        organization_id=context.organization.id,
                        membership_id=context.membership.id,
                        command_type=ROLE_SWITCH_COMMAND,
                        idempotency_key=idempotency_key,
                        payload_digest=payload_digest,
                        response_status=status.HTTP_200_OK,
                        response_body=safe_result,
                        result_membership_id=target_membership.id,
                        result_session_id=replacement.id,
                        now=now,
                    )

    database.commit()
    _set_session_cookie(
        response,
        settings=settings,
        raw_token=replacement_token,
        expires_at=context.organization.expires_at,
        now=now,
    )
    return result
