"""Transactional, retry-safe commands for exception cases."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.session import AuthContext
from app.domain.case_state import (
    InvalidCaseTransition,
    state_after_assignment,
    state_after_resolution,
    validate_note_state,
)
from app.domain.resolution_reasons import resolution_reason_is_allowed
from app.models import CaseNote, ExceptionCase, Membership, Organization
from app.repositories.audit import append_audit_event
from app.services.idempotent_commands import (
    canonical_json_digest,
    find_command_receipt,
    replay_command,
    store_command_receipt,
)

ASSIGNMENT_COMMAND = "case.assignment"
NOTE_COMMAND = "case.note"
RESOLUTION_COMMAND = "case.resolution"
REPLAY_CONTENTION_CONSTRAINTS = frozenset(
    {
        "uq_audit_events_organization_action_key",
        "uq_command_receipts_membership_command_key",
    }
)
SQLITE_REPLAY_CONTENTION_MESSAGES = frozenset(
    {
        ("UNIQUE constraint failed: audit_events.organization_id, audit_events.action_key"),
        (
            "UNIQUE constraint failed: command_receipts.membership_id, "
            "command_receipts.command_type, command_receipts.idempotency_key"
        ),
    }
)


class CaseNotFoundError(Exception):
    """The case is outside the actor's visible tenant/record scope."""


class AssigneeNotFoundError(Exception):
    """The target is not an Agent in the actor's organization."""


class CaseVersionConflictError(Exception):
    """Another command changed the case after the client read it."""


class CaseTransitionConflictError(Exception):
    """The current case state does not accept this command."""


class ResolutionReasonInvalidError(Exception):
    """The submitted resolution reason is not valid for this rule."""


class DemoCaseNoteLimitExceeded(Exception):
    """The temporary workspace has reached its durable note allowance."""


def _action_key(*, membership_id: str, command_type: str, key: str) -> str:
    digest = hashlib.sha256(f"{membership_id}:{command_type}:{key}".encode()).hexdigest()
    return f"command:{digest}"


def _replay(
    db: Session,
    *,
    context: AuthContext,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
) -> dict[str, Any] | None:
    receipt = find_command_receipt(
        db,
        membership_id=context.membership.id,
        command_type=command_type,
        idempotency_key=idempotency_key,
    )
    if receipt is None:
        return None
    return replay_command(receipt, payload_digest=payload_digest).response_body


def _store_result(
    db: Session,
    *,
    context: AuthContext,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
    response_body: dict[str, Any],
    now: datetime,
) -> None:
    store_command_receipt(
        db,
        organization_id=context.organization.id,
        membership_id=context.membership.id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        response_status=200,
        response_body=response_body,
        result_membership_id=context.membership.id,
        result_session_id=context.session.id,
        now=now,
    )


def _replay_after_contention(
    db: Session,
    *,
    context: AuthContext,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
) -> dict[str, Any]:
    db.rollback()
    replayed = _replay(
        db,
        context=context,
        command_type=command_type,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
    )
    if replayed is not None:
        return replayed
    raise CaseVersionConflictError


def _replay_after_integrity_contention(
    db: Session,
    *,
    context: AuthContext,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
    error: IntegrityError,
) -> dict[str, Any]:
    """Resolve a command-key race after the losing transaction rolls back."""

    db.rollback()
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    is_named_contention = (
        isinstance(constraint_name, str) and constraint_name in REPLAY_CONTENTION_CONSTRAINTS
    )
    is_sqlite_contention = str(error.orig) in SQLITE_REPLAY_CONTENTION_MESSAGES
    if not is_named_contention and not is_sqlite_contention:
        raise error

    replayed = _replay(
        db,
        context=context,
        command_type=command_type,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
    )
    if replayed is not None:
        return replayed
    raise error


def _complete_case_command(
    db: Session,
    *,
    context: AuthContext,
    case_id: str,
    version: int,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
    now: datetime,
) -> dict[str, Any]:
    response_body = {"case_id": case_id, "version": version}
    _store_result(
        db,
        context=context,
        command_type=command_type,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        response_body=response_body,
        now=now,
    )
    db.commit()
    return response_body


def _consume_demo_note_quota(
    db: Session,
    *,
    context: AuthContext,
    maximum: int,
) -> None:
    if not context.organization.is_demo:
        return

    result = db.execute(
        update(Organization)
        .where(
            Organization.id == context.organization.id,
            Organization.is_demo.is_(True),
            Organization.case_note_count < maximum,
        )
        .values(case_note_count=Organization.case_note_count + 1)
        .execution_options(synchronize_session=False)
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        raise DemoCaseNoteLimitExceeded


def _lock_organization_for_case_write(db: Session, *, context: AuthContext) -> None:
    """Keep case commands on the same parent-to-child lock order as workspace deletion."""

    organization_id = db.scalar(
        select(Organization.id)
        .where(Organization.id == context.organization.id)
        .with_for_update(read=True, key_share=True)
    )
    if organization_id is None:
        raise CaseNotFoundError


def assign_case(
    db: Session,
    *,
    context: AuthContext,
    case_id: str,
    assignee_id: str,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
) -> dict[str, Any]:
    payload_digest = canonical_json_digest(
        {"case_id": case_id, "assignee_id": assignee_id, "version": expected_version}
    )
    replayed = _replay(
        db,
        context=context,
        command_type=ASSIGNMENT_COMMAND,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
    )
    if replayed is not None:
        return replayed

    _lock_organization_for_case_write(db, context=context)

    target_agent = db.scalar(
        select(Membership).where(
            Membership.organization_id == context.organization.id,
            Membership.id == assignee_id,
            Membership.role == "agent",
        )
    )
    if target_agent is None:
        raise AssigneeNotFoundError
    case = db.scalar(
        select(ExceptionCase).where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.id == case_id,
        )
    )
    if case is None:
        raise CaseNotFoundError
    try:
        next_status = state_after_assignment(case.status)
    except InvalidCaseTransition:
        raise CaseTransitionConflictError from None
    if case.version != expected_version:
        raise CaseVersionConflictError

    previous_status = case.status
    previous_assignee_id = case.assignee_membership_id
    if previous_assignee_id == assignee_id:
        return {"case_id": case_id, "version": expected_version}

    result = db.execute(
        update(ExceptionCase)
        .where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.id == case_id,
            ExceptionCase.version == expected_version,
            ExceptionCase.status.in_(("open", "assigned")),
        )
        .values(
            status=next_status,
            assignee_membership_id=assignee_id,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        return _replay_after_contention(
            db,
            context=context,
            command_type=ASSIGNMENT_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    try:
        append_audit_event(
            db,
            organization_id=context.organization.id,
            actor_membership_id=context.membership.id,
            action_key=_action_key(
                membership_id=context.membership.id,
                command_type=ASSIGNMENT_COMMAND,
                key=idempotency_key,
            ),
            action="case.assigned" if previous_assignee_id is None else "case.reassigned",
            object_type="case",
            object_id=case_id,
            object_version=expected_version + 1,
            changes={
                "from_assignee_id": previous_assignee_id,
                "from_status": previous_status,
                "to_assignee_id": assignee_id,
                "to_status": next_status,
                "version": expected_version + 1,
            },
            now=now,
        )
        return _complete_case_command(
            db,
            context=context,
            case_id=case_id,
            version=expected_version + 1,
            command_type=ASSIGNMENT_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            now=now,
        )
    except IntegrityError as error:
        return _replay_after_integrity_contention(
            db,
            context=context,
            command_type=ASSIGNMENT_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            error=error,
        )


def add_case_note(
    db: Session,
    *,
    context: AuthContext,
    case_id: str,
    body: str,
    expected_version: int,
    idempotency_key: str,
    note_limit: int,
    now: datetime,
) -> dict[str, Any]:
    payload_digest = canonical_json_digest(
        {"case_id": case_id, "body": body, "version": expected_version}
    )
    replayed = _replay(
        db,
        context=context,
        command_type=NOTE_COMMAND,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
    )
    if replayed is not None:
        return replayed

    case = db.scalar(
        select(ExceptionCase).where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.id == case_id,
        )
    )
    if case is None or (
        context.membership.role == "agent" and case.assignee_membership_id != context.membership.id
    ):
        raise CaseNotFoundError
    try:
        validate_note_state(case.status)
    except InvalidCaseTransition:
        raise CaseTransitionConflictError from None
    if case.version != expected_version:
        raise CaseVersionConflictError

    try:
        _consume_demo_note_quota(db, context=context, maximum=note_limit)
    except DemoCaseNoteLimitExceeded:
        db.rollback()
        replayed = _replay(
            db,
            context=context,
            command_type=NOTE_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )
        if replayed is not None:
            return replayed
        raise
    result = db.execute(
        update(ExceptionCase)
        .where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.id == case_id,
            ExceptionCase.version == expected_version,
            ExceptionCase.status.in_(("open", "assigned")),
        )
        .values(version=expected_version + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        return _replay_after_contention(
            db,
            context=context,
            command_type=NOTE_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    note = CaseNote(
        id=str(uuid4()),
        organization_id=context.organization.id,
        case_id=case_id,
        author_membership_id=context.membership.id,
        body=body,
        created_at=now,
    )
    db.add(note)
    db.flush()
    try:
        append_audit_event(
            db,
            organization_id=context.organization.id,
            actor_membership_id=context.membership.id,
            action_key=_action_key(
                membership_id=context.membership.id,
                command_type=NOTE_COMMAND,
                key=idempotency_key,
            ),
            action="case.note_added",
            object_type="case",
            object_id=case_id,
            object_version=expected_version + 1,
            changes={"note_id": note.id, "version": expected_version + 1},
            now=now,
        )
        return _complete_case_command(
            db,
            context=context,
            case_id=case_id,
            version=expected_version + 1,
            command_type=NOTE_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            now=now,
        )
    except IntegrityError as error:
        return _replay_after_integrity_contention(
            db,
            context=context,
            command_type=NOTE_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            error=error,
        )


def resolve_case(
    db: Session,
    *,
    context: AuthContext,
    case_id: str,
    reason: str,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
) -> dict[str, Any]:
    payload_digest = canonical_json_digest(
        {"case_id": case_id, "reason": reason, "version": expected_version}
    )
    replayed = _replay(
        db,
        context=context,
        command_type=RESOLUTION_COMMAND,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
    )
    if replayed is not None:
        return replayed

    _lock_organization_for_case_write(db, context=context)

    case = db.scalar(
        select(ExceptionCase).where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.id == case_id,
        )
    )
    agent_can_resolve = (
        case is not None
        and case.status == "assigned"
        and case.assignee_membership_id == context.membership.id
    )
    if case is None or (context.membership.role == "agent" and not agent_can_resolve):
        raise CaseNotFoundError
    try:
        next_status = state_after_resolution(case.status)
    except InvalidCaseTransition:
        raise CaseTransitionConflictError from None
    if case.version != expected_version:
        raise CaseVersionConflictError
    if not resolution_reason_is_allowed(case.rule_key, reason):
        raise ResolutionReasonInvalidError

    previous_status = case.status
    result = db.execute(
        update(ExceptionCase)
        .where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.id == case_id,
            ExceptionCase.version == expected_version,
            ExceptionCase.status.in_(("open", "assigned")),
        )
        .values(
            status=next_status,
            resolution_reason=reason,
            resolved_at=now,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        return _replay_after_contention(
            db,
            context=context,
            command_type=RESOLUTION_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    try:
        append_audit_event(
            db,
            organization_id=context.organization.id,
            actor_membership_id=context.membership.id,
            action_key=_action_key(
                membership_id=context.membership.id,
                command_type=RESOLUTION_COMMAND,
                key=idempotency_key,
            ),
            action="case.resolved",
            object_type="case",
            object_id=case_id,
            object_version=expected_version + 1,
            changes={
                "from_status": previous_status,
                "resolution_reason": reason,
                "to_status": next_status,
                "version": expected_version + 1,
            },
            now=now,
        )
        return _complete_case_command(
            db,
            context=context,
            case_id=case_id,
            version=expected_version + 1,
            command_type=RESOLUTION_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            now=now,
        )
    except IntegrityError as error:
        return _replay_after_integrity_contention(
            db,
            context=context,
            command_type=RESOLUTION_COMMAND,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            error=error,
        )
