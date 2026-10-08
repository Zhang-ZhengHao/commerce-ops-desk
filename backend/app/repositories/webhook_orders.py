"""Conflict-safe, transaction-neutral order primitives for webhooks."""

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

from app.models import Order

ORDER_IDENTITY_COLUMNS = (Order.organization_id, Order.external_order_id)


@dataclass(frozen=True)
class WebhookOrderClaim:
    order: Order
    inserted: bool


def get_or_create_webhook_order(
    db: Session,
    *,
    organization_id: str,
    external_order_id: str,
    order_number: str,
    amount_minor: int,
    currency: str,
    now: datetime,
) -> WebhookOrderClaim:
    """Insert an unseen tenant order or return the committed winner."""

    order_id = str(uuid4())
    values = {
        "id": order_id,
        "organization_id": organization_id,
        "external_order_id": external_order_id,
        "order_number": order_number,
        "amount_minor": amount_minor,
        "currency": currency,
        "payment_status": "pending",
        "fulfillment_status": "unfulfilled",
        "created_at": now,
        "updated_at": now,
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_statement = sqlite_insert(Order).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_nothing(
            index_elements=ORDER_IDENTITY_COLUMNS
        )
        result = db.execute(sqlite_statement)
        inserted = cast(CursorResult[Any], result).rowcount == 1
    elif dialect_name == "postgresql":
        postgresql_statement = postgresql_insert(Order).values(**values)
        postgresql_statement = postgresql_statement.on_conflict_do_nothing(
            constraint="uq_orders_organization_external_order"
        )
        inserted = db.scalar(postgresql_statement.returning(Order.id)) is not None
    else:  # Settings prevents unsupported database backends.
        raise RuntimeError("unsupported webhook database")

    order = db.scalar(
        select(Order).where(
            Order.organization_id == organization_id,
            Order.external_order_id == external_order_id,
        )
    )
    if order is None:
        raise RuntimeError("webhook order claim winner is unavailable")
    return WebhookOrderClaim(order=order, inserted=inserted)


def mark_webhook_order_payment_failed(
    db: Session,
    *,
    organization_id: str,
    order_id: str,
    now: datetime,
) -> Order:
    """Apply the payment failure while retaining caller transaction ownership."""

    result = db.execute(
        update(Order)
        .where(
            Order.organization_id == organization_id,
            Order.id == order_id,
        )
        .values(payment_status="failed", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        raise RuntimeError("webhook order disappeared during processing")
    order = db.scalar(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.id == order_id,
        )
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise RuntimeError("webhook order is unavailable after update")
    return order
