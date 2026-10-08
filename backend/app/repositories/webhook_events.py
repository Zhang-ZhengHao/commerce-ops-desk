"""Atomic, transaction-neutral persistence for the webhook inbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import Organization, WebhookEvent, WebhookIntegration

EVENT_IDENTITY_COLUMNS = (
    WebhookEvent.organization_id,
    WebhookEvent.integration_id,
    WebhookEvent.external_event_id,
)


@dataclass(frozen=True)
class WebhookEventClaim:
    event: WebhookEvent
    inserted: bool


def lock_active_demo_webhook_target(
    db: Session,
    *,
    organization_id: str,
    integration_id: str,
    integration_key_version: int,
    now: datetime,
) -> Organization | None:
    """Revalidate a bound target while locking its parent before child writes."""

    return db.scalar(
        select(Organization)
        .join(
            WebhookIntegration,
            (WebhookIntegration.organization_id == Organization.id)
            & (WebhookIntegration.id == integration_id),
        )
        .where(
            Organization.id == organization_id,
            Organization.is_demo.is_(True),
            Organization.expires_at > now,
            WebhookIntegration.provider == "synthetic",
            WebhookIntegration.enabled.is_(True),
            WebhookIntegration.key_version == integration_key_version,
        )
        .with_for_update(key_share=True, of=Organization)
    )


def consume_demo_webhook_event_allowance(
    db: Session,
    *,
    organization_id: str,
    maximum: int,
) -> bool:
    """Atomically consume one new-event slot without owning the transaction."""

    result = db.execute(
        update(Organization)
        .where(
            Organization.id == organization_id,
            Organization.is_demo.is_(True),
            Organization.webhook_event_count < maximum,
        )
        .values(webhook_event_count=Organization.webhook_event_count + 1)
    )
    return cast(CursorResult[Any], result).rowcount == 1


def claim_webhook_event(
    db: Session,
    *,
    organization_id: str,
    integration_id: str,
    external_event_id: str,
    event_type: str,
    payload_digest: str,
    occurred_at: datetime,
    received_at: datetime,
) -> WebhookEventClaim:
    """Insert one tenant-scoped inbox claim or return its committed winner."""

    event_id = str(uuid4())
    values = {
        "id": event_id,
        "organization_id": organization_id,
        "integration_id": integration_id,
        "external_event_id": external_event_id,
        "event_type": event_type,
        "payload_digest": payload_digest,
        "occurred_at": occurred_at,
        "order_id": None,
        "case_id": None,
        "received_at": received_at,
        "processed_at": None,
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_statement = sqlite_insert(WebhookEvent).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_nothing(
            index_elements=EVENT_IDENTITY_COLUMNS
        )
        result = db.execute(sqlite_statement)
        inserted = cast(CursorResult[Any], result).rowcount == 1
    elif dialect_name == "postgresql":
        postgresql_statement = postgresql_insert(WebhookEvent).values(**values)
        postgresql_statement = postgresql_statement.on_conflict_do_nothing(
            constraint="uq_webhook_events_organization_integration_external_event"
        )
        inserted = db.scalar(postgresql_statement.returning(WebhookEvent.id)) is not None
    else:  # Settings prevents unsupported database backends.
        raise RuntimeError("unsupported webhook database")

    event = db.scalar(
        select(WebhookEvent).where(
            WebhookEvent.organization_id == organization_id,
            WebhookEvent.integration_id == integration_id,
            WebhookEvent.external_event_id == external_event_id,
        )
    )
    if event is None:
        raise RuntimeError("webhook event claim winner is unavailable")
    return WebhookEventClaim(event=event, inserted=inserted)


def complete_webhook_event(
    db: Session,
    *,
    organization_id: str,
    event_id: str,
    order_id: str,
    case_id: str,
    processed_at: datetime,
) -> WebhookEvent:
    """Attach all result references in one guarded update without committing."""

    result = db.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.organization_id == organization_id,
            WebhookEvent.id == event_id,
            WebhookEvent.order_id.is_(None),
            WebhookEvent.case_id.is_(None),
            WebhookEvent.processed_at.is_(None),
        )
        .values(
            order_id=order_id,
            case_id=case_id,
            processed_at=processed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        raise RuntimeError("webhook event completion lost its claim")
    event = db.scalar(
        select(WebhookEvent)
        .where(
            WebhookEvent.organization_id == organization_id,
            WebhookEvent.id == event_id,
        )
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise RuntimeError("webhook event is unavailable after completion")
    return event
