"""Case reads always require an authenticated organization context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.auth.session import AuthContext
from app.models import ExceptionCase, Membership, Order, User

CaseStatus = Literal["open", "assigned", "resolved"]
CaseSeverity = Literal["high", "medium"]
CaseSort = Literal["due_at", "-due_at", "updated_at", "-updated_at"]


@dataclass(frozen=True)
class CaseRow:
    case: ExceptionCase
    order: Order
    assignee: Membership | None
    assignee_user: User | None


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
        select(ExceptionCase, Order, Membership, User)
        .join(
            Order,
            (Order.organization_id == ExceptionCase.organization_id)
            & (Order.id == ExceptionCase.order_id),
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
            )
            for row in rows
        ],
        total,
    )


def find_case(db: Session, *, context: AuthContext, case_id: str) -> CaseRow | None:
    row = db.execute(
        select(ExceptionCase, Order, Membership, User)
        .join(
            Order,
            (Order.organization_id == ExceptionCase.organization_id)
            & (Order.id == ExceptionCase.order_id),
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
        .where(ExceptionCase.id == case_id, *_visibility(context))
    ).one_or_none()
    if row is None:
        return None
    return CaseRow(case=row[0], order=row[1], assignee=row[2], assignee_user=row[3])
