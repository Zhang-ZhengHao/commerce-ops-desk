"""Small synthetic dataset copied into each disposable demo workspace."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict
from uuid import uuid4

from sqlalchemy.orm import Session

from app.domain.case_rules import CASE_RULES
from app.models import ExceptionCase, Membership, Order, Organization


class SeedSpecification(TypedDict):
    number: str
    amount: int
    rule_key: str
    status: str
    assignee_id: str | None
    resolution_reason: str | None


def seed_demo_order_cases(
    db: Session,
    *,
    organization: Organization,
    agent_membership: Membership,
    now: datetime,
) -> None:
    """Create only fictional, provider-neutral records for the guided demo."""
    specifications: tuple[SeedSpecification, ...] = (
        {
            "number": "DEMO-1041",
            "amount": 8_900,
            "rule_key": "payment_failed",
            "status": "resolved",
            "assignee_id": None,
            "resolution_reason": "payment_recovered",
        },
        {
            "number": "DEMO-1042",
            "amount": 12_500,
            "rule_key": "payment_failed",
            "status": "assigned",
            "assignee_id": agent_membership.id,
            "resolution_reason": None,
        },
        {
            "number": "DEMO-1043",
            "amount": 6_400,
            "rule_key": "refund_review",
            "status": "open",
            "assignee_id": None,
            "resolution_reason": None,
        },
        {
            "number": "DEMO-1044",
            "amount": 15_750,
            "rule_key": "fulfillment_delayed",
            "status": "open",
            "assignee_id": None,
            "resolution_reason": None,
        },
    )
    for index, specification in enumerate(specifications, start=1):
        order_id = str(uuid4())
        rule_key = specification["rule_key"]
        rule = CASE_RULES[rule_key]
        status = specification["status"]
        order = Order(
            id=order_id,
            organization_id=organization.id,
            external_order_id=f"seed-order-{index}",
            order_number=specification["number"],
            amount_minor=specification["amount"],
            currency="USD",
            payment_status="failed" if rule_key == "payment_failed" else "paid",
            fulfillment_status=("delayed" if rule_key == "fulfillment_delayed" else "unfulfilled"),
            created_at=now,
            updated_at=now,
        )
        db.add(order)
        db.flush()
        db.add(
            ExceptionCase(
                id=str(uuid4()),
                organization_id=organization.id,
                order_id=order.id,
                source_event_id=f"seed-event-{index}",
                rule_key=rule_key,
                case_type=rule.case_type,
                severity=rule.severity,
                status=status,
                assignee_membership_id=specification["assignee_id"],
                due_at=now + rule.sla,
                resolution_reason=specification["resolution_reason"],
                resolved_at=now if status == "resolved" else None,
                version=2 if status == "resolved" else 1,
                created_at=now,
                updated_at=now,
            )
        )
    db.flush()
