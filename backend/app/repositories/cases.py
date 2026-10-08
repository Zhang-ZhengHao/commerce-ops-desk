"""Case reads always require an authenticated organization context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.auth.session import AuthContext
from app.models import (
    ExceptionCase,
    Membership,
    Order,
    User,
    WebhookEvent,
    WebhookIntegration,
)

CaseStatus = Literal["open", "assigned", "resolved"]
CaseSeverity = Literal["high", "medium"]
CaseSort = Literal["due_at", "-due_at", "updated_at", "-updated_at"]


@dataclass(frozen=True)
class SyntheticWebhookSourceRow:
    provider: str
    event_type: str
    external_event_id: str
    received_at: datetime


@dataclass(frozen=True)
class CaseRow:
    case: ExceptionCase
    order: Order
    assignee: Membership | None
    assignee_user: User | None
    source: SyntheticWebhookSourceRow | None


@dataclass(frozen=True)
class NoteRow:
    note_id: str
    body: str
    created_at: object
    author: Membership
    author_user: User


def _visibility(context: AuthContext) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        ExceptionCase.organization_id == context.organization.id
    ]
    if context.membership.role == "agent":
        conditions.append(ExceptionCase.assignee_membership_id == context.membership.id)
    return conditions


def _source_row(
    *,
    provider: str | None,
    event_type: str | None,
    external_event_id: str | None,
    received_at: datetime | None,
) -> SyntheticWebhookSourceRow | None:
    values = (provider, event_type, external_event_id, received_at)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise RuntimeError("webhook case provenance is incomplete")
    assert provider is not None
    assert event_type is not None
    assert external_event_id is not None
    assert received_at is not None
    return SyntheticWebhookSourceRow(
        provider=provider,
        event_type=event_type,
        external_event_id=external_event_id,
        received_at=received_at,
    )


def _case_read_statement() -> Select[
    ExceptionCase,
    Order,
    Membership,
    User,
    str,
    str,
    str,
    datetime,
]:
    return (
        select(
            ExceptionCase,
            Order,
            Membership,
            User,
            WebhookIntegration.provider,
            WebhookEvent.event_type,
            WebhookEvent.external_event_id,
            WebhookEvent.received_at,
        )
        .join(
            Order,
            (Order.organization_id == ExceptionCase.organization_id)
            & (Order.id == ExceptionCase.order_id),
        )
        .outerjoin(
            WebhookEvent,
            (WebhookEvent.organization_id == ExceptionCase.organization_id)
            & (WebhookEvent.id == ExceptionCase.source_event_id)
            & (WebhookEvent.case_id == ExceptionCase.id)
            & (WebhookEvent.order_id == ExceptionCase.order_id),
        )
        .outerjoin(
            WebhookIntegration,
            (WebhookIntegration.organization_id == ExceptionCase.organization_id)
            & (WebhookIntegration.id == WebhookEvent.integration_id),
        )
        .outerjoin(
            Membership,
            (Membership.organization_id == ExceptionCase.organization_id)
            & (Membership.id == ExceptionCase.assignee_membership_id),
        )
        .outerjoin(
            User,
            (User.organization_id == Membership.organization_id) & (User.id == Membership.user_id),
        )
    )


def list_cases(
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
) -> tuple[list[CaseRow], int]:
    conditions = _visibility(context)
    if status is not None:
        conditions.append(ExceptionCase.status == status)
    if severity is not None:
        conditions.append(ExceptionCase.severity == severity)
    if rule_key is not None:
        conditions.append(ExceptionCase.rule_key == rule_key)
    if assignee_id is not None:
        conditions.append(ExceptionCase.assignee_membership_id == assignee_id)

    total = int(db.scalar(select(func.count(ExceptionCase.id)).where(*conditions)) or 0)
    order_column = (
        ExceptionCase.due_at if sort.lstrip("-") == "due_at" else ExceptionCase.updated_at
    )
    ordering = order_column.desc() if sort.startswith("-") else order_column.asc()
    rows = db.execute(
        _case_read_statement()
        .where(*conditions)
        .order_by(ordering, ExceptionCase.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return (
        [
            CaseRow(
                case=row[0],
                order=row[1],
                assignee=row[2],
                assignee_user=row[3],
                source=_source_row(
                    provider=row[4],
                    event_type=row[5],
                    external_event_id=row[6],
                    received_at=row[7],
                ),
            )
            for row in rows
        ],
        total,
    )


def find_case(db: Session, *, context: AuthContext, case_id: str) -> CaseRow | None:
    row = db.execute(
        _case_read_statement().where(ExceptionCase.id == case_id, *_visibility(context))
    ).one_or_none()
    if row is None:
        return None
    return CaseRow(
        case=row[0],
        order=row[1],
        assignee=row[2],
        assignee_user=row[3],
        source=_source_row(
            provider=row[4],
            event_type=row[5],
            external_event_id=row[6],
            received_at=row[7],
        ),
    )
