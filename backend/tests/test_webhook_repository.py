"""Transaction-neutral persistence primitives for the webhook processor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import cast
from uuid import uuid4

from conftest import AppHarness
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.case_rules import CASE_RULES
from app.models import ExceptionCase, Order, Organization, WebhookEvent, WebhookIntegration

NOW = datetime(2026, 10, 8, 9, 15, 0, tzinfo=UTC)
PAYLOAD_DIGEST = "a" * 64


def _seed_target(database: Session) -> tuple[str, str]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    database.add(
        Organization(
            id=organization_id,
            name="Webhook repository test workspace",
            is_demo=True,
            case_note_count=0,
            webhook_event_count=0,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=4),
        )
    )
    database.add(
        WebhookIntegration(
            id=integration_id,
            organization_id=organization_id,
            provider="synthetic",
            key_version=1,
            enabled=True,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    database.commit()
    return organization_id, integration_id


def _seed_order_case(database: Session, *, organization_id: str) -> tuple[str, str]:
    order_id = str(uuid4())
    case_id = str(uuid4())
    database.add(
        Order(
            id=order_id,
            organization_id=organization_id,
            external_order_id="seed-order-for-inbox-completion",
            order_number="DEMO-9998",
            amount_minor=1_000,
            currency="USD",
            payment_status="failed",
            fulfillment_status="unfulfilled",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    database.flush()
    rule = CASE_RULES["payment_failed"]
    database.add(
        ExceptionCase(
            id=case_id,
            organization_id=organization_id,
            order_id=order_id,
            source_event_id="seed-source-for-inbox-completion",
            rule_key="payment_failed",
            case_type=rule.case_type,
            severity=rule.severity,
            status="open",
            assignee_membership_id=None,
            due_at=NOW + rule.sla,
            resolution_reason=None,
            resolved_at=None,
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    database.commit()
    return order_id, case_id


def test_event_claim_is_visible_inside_the_caller_transaction_and_rolls_back(
    app_harness: AppHarness,
) -> None:
    repository = import_module("app.repositories.webhook_events")
    claim_webhook_event = repository.claim_webhook_event
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        claim = claim_webhook_event(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_REPOCLAIM01",
            event_type="payment.failed",
            payload_digest=PAYLOAD_DIGEST,
            occurred_at=NOW - timedelta(minutes=1),
            received_at=NOW,
        )

        assert claim.inserted is True
        assert claim.event.organization_id == organization_id
        assert claim.event.integration_id == integration_id
        assert claim.event.external_event_id == "evt_REPOCLAIM01"
        assert claim.event.order_id is None
        assert claim.event.case_id is None
        assert claim.event.processed_at is None
        assert database.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        database.rollback()

    with factory() as database:
        assert database.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_order_claim_is_visible_inside_the_caller_transaction_and_rolls_back(
    app_harness: AppHarness,
) -> None:
    repository = import_module("app.repositories.webhook_orders")
    get_or_create_webhook_order = repository.get_or_create_webhook_order
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, _integration_id = _seed_target(database)
        claim = get_or_create_webhook_order(
            database,
            organization_id=organization_id,
            external_order_id="syn_order_REPOORDER01",
            order_number="DEMO-9001",
            amount_minor=12_345,
            currency="USD",
            now=NOW,
        )

        assert claim.inserted is True
        assert claim.order.organization_id == organization_id
        assert claim.order.external_order_id == "syn_order_REPOORDER01"
        assert claim.order.order_number == "DEMO-9001"
        assert claim.order.amount_minor == 12_345
        assert claim.order.currency == "USD"
        assert claim.order.payment_status == "pending"
        assert claim.order.fulfillment_status == "unfulfilled"
        assert database.scalar(select(func.count()).select_from(Order)) == 1
        database.rollback()

    with factory() as database:
        assert database.scalar(select(func.count()).select_from(Order)) == 0


def test_marking_payment_failed_remains_owned_by_the_caller_transaction(
    app_harness: AppHarness,
) -> None:
    repository = import_module("app.repositories.webhook_orders")
    get_or_create_webhook_order = repository.get_or_create_webhook_order
    mark_webhook_order_payment_failed = repository.mark_webhook_order_payment_failed
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, _integration_id = _seed_target(database)
        order = get_or_create_webhook_order(
            database,
            organization_id=organization_id,
            external_order_id="syn_order_REPOMARK01",
            order_number="DEMO-9002",
            amount_minor=54_321,
            currency="USD",
            now=NOW,
        ).order
        database.commit()
        order_id = order.id

    changed_at = NOW + timedelta(seconds=5)
    with factory() as database:
        changed = mark_webhook_order_payment_failed(
            database,
            organization_id=organization_id,
            order_id=order_id,
            now=changed_at,
        )
        assert changed.payment_status == "failed"
        assert changed.updated_at == changed_at.replace(tzinfo=None)
        database.rollback()

    with factory() as database:
        persisted = database.get(Order, order_id)
        assert persisted is not None
        assert persisted.payment_status == "pending"
        assert persisted.updated_at == NOW.replace(tzinfo=None)


def test_marking_a_just_claimed_order_refreshes_the_session_identity(
    app_harness: AppHarness,
) -> None:
    repository = import_module("app.repositories.webhook_orders")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, _integration_id = _seed_target(database)
        claim = repository.get_or_create_webhook_order(
            database,
            organization_id=organization_id,
            external_order_id="syn_order_REPOIDENTITY1",
            order_number="DEMO-9003",
            amount_minor=22_222,
            currency="USD",
            now=NOW,
        )
        changed_at = NOW + timedelta(seconds=8)

        changed = repository.mark_webhook_order_payment_failed(
            database,
            organization_id=organization_id,
            order_id=claim.order.id,
            now=changed_at,
        )

        assert changed is claim.order
        assert changed.payment_status == "failed"
        assert changed.updated_at == changed_at.replace(tzinfo=None)


def test_completing_an_event_remains_owned_by_the_caller_transaction(
    app_harness: AppHarness,
) -> None:
    repository = import_module("app.repositories.webhook_events")
    claim_webhook_event = repository.claim_webhook_event
    complete_webhook_event = repository.complete_webhook_event
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        order_id, case_id = _seed_order_case(database, organization_id=organization_id)
        event = claim_webhook_event(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_REPOCOMPLETE1",
            event_type="payment.failed",
            payload_digest=PAYLOAD_DIGEST,
            occurred_at=NOW - timedelta(minutes=1),
            received_at=NOW,
        ).event

        completed = complete_webhook_event(
            database,
            organization_id=organization_id,
            event_id=event.id,
            order_id=order_id,
            case_id=case_id,
            processed_at=NOW + timedelta(seconds=2),
        )

        assert completed.order_id == order_id
        assert completed.case_id == case_id
        assert completed.processed_at == (NOW + timedelta(seconds=2)).replace(tzinfo=None)
        database.rollback()

    with factory() as database:
        assert database.scalar(select(func.count()).select_from(WebhookEvent)) == 0
