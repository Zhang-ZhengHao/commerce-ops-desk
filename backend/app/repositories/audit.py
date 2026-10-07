"""Read and append boundaries for immutable application audit events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.auth.session import AuthContext
from app.models import AuditEvent, ExceptionCase, Membership, User


@dataclass(frozen=True)
class AuditRow:
    event: AuditEvent
    actor: Membership | None
    actor_user: User | None


def append_audit_event(
    db: Session,
    *,
    organization_id: str,
    actor_membership_id: str | None,
    action_key: str,
    action: str,
    object_type: str,
    object_id: str,
    object_version: int | None,
    changes: Mapping[str, object],
    now: datetime,
) -> AuditEvent:
    event = AuditEvent(
        id=str(uuid4()),
        organization_id=organization_id,
        actor_membership_id=actor_membership_id,
        action_key=action_key,
        action=action,
        object_type=object_type,
        object_id=object_id,
        object_version=object_version,
        changes_json=json.dumps(
            changes,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        created_at=now,
    )
    db.add(event)
    db.flush()
    return event


def list_case_audit_events(
    db: Session,
    *,
    organization_id: str,
    case_id: str,
) -> list[AuditRow]:
    rows = db.execute(
        select(AuditEvent, Membership, User)
        .outerjoin(
            Membership,
            (Membership.organization_id == AuditEvent.organization_id)
            & (Membership.id == AuditEvent.actor_membership_id),
        )
        .outerjoin(
            User,
            (User.organization_id == Membership.organization_id) & (User.id == Membership.user_id),
        )
        .where(
            AuditEvent.organization_id == organization_id,
            AuditEvent.object_type == "case",
            AuditEvent.object_id == case_id,
        )
        .order_by(
            AuditEvent.object_version.asc().nulls_last(),
            AuditEvent.created_at,
            AuditEvent.id,
        )
    ).all()
    return [AuditRow(event=row[0], actor=row[1], actor_user=row[2]) for row in rows]


def list_audit_events(
    db: Session,
    *,
    context: AuthContext,
    case_id: str | None,
    page: int,
    page_size: int,
) -> tuple[list[AuditRow], int]:
    conditions: list[ColumnElement[bool]] = [AuditEvent.organization_id == context.organization.id]
    if case_id is not None:
        conditions.extend((AuditEvent.object_type == "case", AuditEvent.object_id == case_id))
    if context.membership.role == "agent":
        visible_case_ids = select(ExceptionCase.id).where(
            ExceptionCase.organization_id == context.organization.id,
            ExceptionCase.assignee_membership_id == context.membership.id,
        )
        conditions.extend(
            (
                AuditEvent.object_type == "case",
                AuditEvent.object_id.in_(visible_case_ids),
            )
        )

    total = int(db.scalar(select(func.count(AuditEvent.id)).where(*conditions)) or 0)
    rows = db.execute(
        select(AuditEvent, Membership, User)
        .outerjoin(
            Membership,
            (Membership.organization_id == AuditEvent.organization_id)
            & (Membership.id == AuditEvent.actor_membership_id),
        )
        .outerjoin(
            User,
            (User.organization_id == Membership.organization_id) & (User.id == Membership.user_id),
        )
        .where(*conditions)
        .order_by(
            AuditEvent.created_at.desc(),
            AuditEvent.object_version.desc().nulls_last(),
            AuditEvent.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [AuditRow(event=row[0], actor=row[1], actor_user=row[2]) for row in rows], total
