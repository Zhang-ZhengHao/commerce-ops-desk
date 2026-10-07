"""Tenant- and role-scoped read models for case workflow screens."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.session import AuthContext, as_utc
from app.domain.case_rules import CASE_RULES
from app.models import CaseNote, ExceptionCase, Membership, User
from app.repositories.audit import AuditRow, list_case_audit_events
from app.repositories.cases import (
    CaseRow,
    CaseSeverity,
    CaseSort,
    CaseStatus,
    find_case,
    list_cases,
)


def _case_summary(row: CaseRow) -> dict[str, Any]:
    assignee = None
    if row.assignee is not None and row.assignee_user is not None:
        assignee = {
            "membership_id": row.assignee.id,
            "display_name": row.assignee_user.display_name,
        }
    return {
        "id": row.case.id,
        "rule_key": row.case.rule_key,
        "case_type": row.case.case_type,
        "severity": row.case.severity,
        "status": row.case.status,
        "due_at": as_utc(row.case.due_at),
        "updated_at": as_utc(row.case.updated_at),
        "version": row.case.version,
        "resolution_reason": row.case.resolution_reason,
        "resolved_at": (as_utc(row.case.resolved_at) if row.case.resolved_at is not None else None),
        "order": {
            "id": row.order.id,
            "order_number": row.order.order_number,
            "amount_minor": row.order.amount_minor,
            "currency": row.order.currency,
            "payment_status": row.order.payment_status,
            "fulfillment_status": row.order.fulfillment_status,
        },
        "assignee": assignee,
    }


def case_queue_payload(
    db: Session,
    *,
    context: AuthContext,
    status: CaseStatus | None,
    severity: CaseSeverity | None,
    rule_key: str | None,
    assignee_id: str | None,
    page: int,
    page_size: int,
    sort: CaseSort,
) -> dict[str, Any]:
    rows, total = list_cases(
        db,
        context=context,
        status=status,
        severity=severity,
        rule_key=rule_key,
        assignee_id=assignee_id,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return {
        "items": [_case_summary(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _audit_payload(row: AuditRow) -> dict[str, Any]:
    actor = None
    if row.actor is not None and row.actor_user is not None:
        actor = {
            "membership_id": row.actor.id,
            "display_name": row.actor_user.display_name,
        }
    changes = json.loads(row.event.changes_json)
    if not isinstance(changes, dict):
        raise RuntimeError("audit event changes must be an object")
    return {
        "id": row.event.id,
        "action": row.event.action,
        "object_type": row.event.object_type,
        "object_id": row.event.object_id,
        "actor": actor,
        "changes": changes,
        "created_at": as_utc(row.event.created_at),
    }


def case_detail_payload(
    db: Session,
    *,
    context: AuthContext,
    case_id: str,
) -> dict[str, Any] | None:
    row = find_case(db, context=context, case_id=case_id)
    if row is None:
        return None
    note_rows = db.execute(
        select(CaseNote, Membership, User)
        .join(
            Membership,
            (Membership.organization_id == CaseNote.organization_id)
            & (Membership.id == CaseNote.author_membership_id),
        )
        .join(
            User,
            (User.organization_id == Membership.organization_id) & (User.id == Membership.user_id),
        )
        .where(
            CaseNote.organization_id == context.organization.id,
            CaseNote.case_id == case_id,
        )
        .order_by(CaseNote.created_at, CaseNote.id)
    ).all()
    payload = _case_summary(row)
    payload.update(
        {
            "created_at": as_utc(row.case.created_at),
            "resolution_reasons": list(CASE_RULES[row.case.rule_key].resolution_reasons),
            "notes": [
                {
                    "id": note.id,
                    "body": note.body,
                    "author": {
                        "membership_id": author.id,
                        "display_name": author_user.display_name,
                    },
                    "created_at": as_utc(note.created_at),
                }
                for note, author, author_user in note_rows
            ],
            "audit_events": [
                _audit_payload(audit_row)
                for audit_row in list_case_audit_events(
                    db,
                    organization_id=context.organization.id,
                    case_id=case_id,
                )
            ],
        }
    )
    return payload


def assignable_agents_payload(
    db: Session,
    *,
    organization_id: str,
) -> dict[str, Any]:
    rows = db.execute(
        select(Membership, User)
        .join(
            User,
            (User.organization_id == Membership.organization_id) & (User.id == Membership.user_id),
        )
        .where(
            Membership.organization_id == organization_id,
            Membership.role == "agent",
        )
        .order_by(User.display_name, Membership.id)
    ).all()
    return {
        "items": [
            {"membership_id": membership.id, "display_name": user.display_name}
            for membership, user in rows
        ]
    }


def dashboard_payload(
    db: Session,
    *,
    context: AuthContext,
    now: datetime,
) -> dict[str, Any]:
    statement = select(ExceptionCase).where(
        ExceptionCase.organization_id == context.organization.id
    )
    if context.membership.role == "agent":
        statement = statement.where(ExceptionCase.assignee_membership_id == context.membership.id)
    cases = list(db.scalars(statement))
    unresolved = [case for case in cases if case.status != "resolved"]
    approaching_cutoff = as_utc(now) + timedelta(hours=2)
    by_rule: list[dict[str, Any]] = []
    for rule_key in sorted(CASE_RULES):
        count = sum(case.rule_key == rule_key for case in cases)
        if count:
            by_rule.append(
                {
                    "rule_key": rule_key,
                    "case_type": CASE_RULES[rule_key].case_type,
                    "count": count,
                }
            )
    return {
        "generated_at": as_utc(now),
        "summary": {
            "open": len(unresolved),
            "approaching_sla": sum(
                as_utc(case.due_at) <= approaching_cutoff for case in unresolved
            ),
            "high_severity": sum(case.severity == "high" for case in unresolved),
            "resolved": sum(case.status == "resolved" for case in cases),
        },
        "by_rule": by_rule,
    }
