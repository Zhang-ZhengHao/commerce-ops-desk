"""Atomic business processing for authenticated synthetic webhook events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.domain.case_rules import CASE_RULES
from app.domain.webhook_event import SyntheticWebhookEventPayload
from app.models import ExceptionCase
from app.repositories.audit import append_audit_event
from app.repositories.webhook_events import (
    claim_webhook_event,
    complete_webhook_event,
    consume_demo_webhook_event_allowance,
    lock_active_demo_webhook_target,
)
from app.repositories.webhook_orders import (
    get_or_create_webhook_order,
    mark_webhook_order_payment_failed,
)

ProcessingStage = Literal[
    "event_claimed",
    "allowance_consumed",
    "order_claimed",
    "payment_failed",
    "case_created",
    "audit_appended",
    "event_completed",
]
type ProcessingHook = Callable[[ProcessingStage], None]


class WebhookProcessingTargetUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("webhook processing target is unavailable")


class WebhookEventDigestConflict(Exception):
    def __init__(self) -> None:
        super().__init__("webhook event identity conflicts with its payload")


class WebhookEventInvariantViolation(Exception):
    def __init__(self) -> None:
        super().__init__("stored webhook event is incomplete")


class DemoWebhookEventLimitExceeded(Exception):
    def __init__(self) -> None:
        super().__init__("demo webhook event limit reached")


class WebhookOrderSnapshotConflict(Exception):
    def __init__(self) -> None:
        super().__init__("webhook order snapshot conflicts with stored metadata")


@dataclass(frozen=True)
class WebhookProcessingResult:
    event_id: str
    case_id: str
    replayed: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("webhook processing clock must be timezone-aware")
    return value.astimezone(UTC)


def _notify(hook: ProcessingHook | None, stage: ProcessingStage) -> None:
    if hook is not None:
        hook(stage)


def process_payment_failed_webhook(
    db: Session,
    *,
    organization_id: str,
    integration_id: str,
    external_event_id: str,
    payload_digest: str,
    payload: SyntheticWebhookEventPayload,
    event_limit: int,
    received_at: datetime,
    stage_hook: ProcessingHook | None = None,
) -> WebhookProcessingResult:
    """Commit one complete effect or roll every business write back."""

    normalized_received_at = _as_utc(received_at)
    if event_limit < 1:
        raise ValueError("webhook event limit must be positive")

    try:
        organization = lock_active_demo_webhook_target(
            db,
            organization_id=organization_id,
            integration_id=integration_id,
            now=normalized_received_at,
        )
        if organization is None:
            raise WebhookProcessingTargetUnavailable

        claim = claim_webhook_event(
            db,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id=external_event_id,
            event_type=payload.type,
            payload_digest=payload_digest,
            occurred_at=payload.occurred_at,
            received_at=normalized_received_at,
        )
        _notify(stage_hook, "event_claimed")

        if not claim.inserted:
            event = claim.event
            if event.payload_digest != payload_digest:
                raise WebhookEventDigestConflict
            if event.order_id is None or event.case_id is None or event.processed_at is None:
                raise WebhookEventInvariantViolation
            result = WebhookProcessingResult(
                event_id=external_event_id,
                case_id=event.case_id,
                replayed=True,
            )
            db.commit()
            return result

        if not consume_demo_webhook_event_allowance(
            db,
            organization_id=organization_id,
            maximum=event_limit,
        ):
            raise DemoWebhookEventLimitExceeded
        _notify(stage_hook, "allowance_consumed")

        submitted_order = payload.data.order
        order_claim = get_or_create_webhook_order(
            db,
            organization_id=organization_id,
            external_order_id=submitted_order.id,
            order_number=submitted_order.number,
            amount_minor=submitted_order.amount_minor,
            currency=submitted_order.currency,
            now=normalized_received_at,
        )
        order = order_claim.order
        if (
            order.order_number != submitted_order.number
            or order.amount_minor != submitted_order.amount_minor
            or order.currency != submitted_order.currency
        ):
            raise WebhookOrderSnapshotConflict
        _notify(stage_hook, "order_claimed")

        order = mark_webhook_order_payment_failed(
            db,
            organization_id=organization_id,
            order_id=order.id,
            now=normalized_received_at,
        )
        _notify(stage_hook, "payment_failed")

        rule = CASE_RULES["payment_failed"]
        case = ExceptionCase(
            id=str(uuid4()),
            organization_id=organization_id,
            order_id=order.id,
            source_event_id=claim.event.id,
            rule_key="payment_failed",
            case_type=rule.case_type,
            severity=rule.severity,
            status="open",
            assignee_membership_id=None,
            due_at=normalized_received_at + rule.sla,
            resolution_reason=None,
            resolved_at=None,
            version=1,
            created_at=normalized_received_at,
            updated_at=normalized_received_at,
        )
        db.add(case)
        db.flush()
        _notify(stage_hook, "case_created")

        append_audit_event(
            db,
            organization_id=organization_id,
            actor_membership_id=None,
            action_key=f"webhook:{claim.event.id}:case.created",
            action="case.created_from_webhook",
            object_type="case",
            object_id=case.id,
            object_version=1,
            changes={
                "event_id": external_event_id,
                "event_type": payload.type,
                "provider": "synthetic",
                "version": 1,
            },
            now=normalized_received_at,
        )
        _notify(stage_hook, "audit_appended")

        complete_webhook_event(
            db,
            organization_id=organization_id,
            event_id=claim.event.id,
            order_id=order.id,
            case_id=case.id,
            processed_at=normalized_received_at,
        )
        _notify(stage_hook, "event_completed")

        result = WebhookProcessingResult(
            event_id=external_event_id,
            case_id=case.id,
            replayed=False,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
