"""One-commit business processing for authenticated synthetic webhooks."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import import_module
from threading import Barrier
from typing import cast
from uuid import uuid4

import pytest
from conftest import AppHarness
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.webhook_event import SyntheticWebhookEventPayload
from app.models import (
    AuditEvent,
    ExceptionCase,
    Order,
    Organization,
    WebhookEvent,
    WebhookIntegration,
)
from app.services.webhook_processing import WebhookProcessingResult

NOW = datetime(2026, 10, 8, 10, 30, 0, tzinfo=UTC)


def _seed_target(database: Session, *, name: str = "Webhook processing test") -> tuple[str, str]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    database.add(
        Organization(
            id=organization_id,
            name=name,
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


def _payload(
    *,
    order_id: str = "syn_order_PROCESS001",
    number: str = "DEMO-7001",
    amount_minor: int = 18_750,
    currency: str = "USD",
) -> SyntheticWebhookEventPayload:
    return SyntheticWebhookEventPayload.model_validate(
        {
            "type": "payment.failed",
            "occurred_at": "2026-10-08T10:29:00Z",
            "data": {
                "order": {
                    "id": order_id,
                    "number": number,
                    "amount_minor": amount_minor,
                    "currency": currency,
                }
            },
        }
    )


def _digest(marker: str) -> str:
    return hashlib.sha256(marker.encode("ascii")).hexdigest()


def _counts(database: Session, *, organization_id: str) -> tuple[int, int, int, int, int]:
    event_count = int(
        database.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(WebhookEvent.organization_id == organization_id)
        )
        or 0
    )
    order_count = int(
        database.scalar(
            select(func.count()).select_from(Order).where(Order.organization_id == organization_id)
        )
        or 0
    )
    case_count = int(
        database.scalar(
            select(func.count())
            .select_from(ExceptionCase)
            .where(ExceptionCase.organization_id == organization_id)
        )
        or 0
    )
    audit_count = int(
        database.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
        )
        or 0
    )
    organization = database.get(Organization, organization_id)
    assert organization is not None
    return (
        event_count,
        order_count,
        case_count,
        audit_count,
        organization.webhook_event_count,
    )


def test_fresh_event_commits_the_complete_business_effect_once(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        result = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_PROCESSFRESH01",
            payload_digest=_digest("fresh-payment-failure-body"),
            payload=_payload(),
            event_limit=20,
            received_at=NOW,
        )

    assert result.event_id == "evt_PROCESSFRESH01"
    assert result.replayed is False

    with factory() as database:
        organization = database.get(Organization, organization_id)
        event = database.scalar(
            select(WebhookEvent).where(WebhookEvent.organization_id == organization_id)
        )
        order = database.scalar(select(Order).where(Order.organization_id == organization_id))
        case = database.scalar(
            select(ExceptionCase).where(ExceptionCase.organization_id == organization_id)
        )
        audit = database.scalar(
            select(AuditEvent).where(AuditEvent.organization_id == organization_id)
        )

    assert organization is not None
    assert event is not None
    assert order is not None
    assert case is not None
    assert audit is not None
    assert organization.webhook_event_count == 1
    assert event.integration_id == integration_id
    assert event.external_event_id == "evt_PROCESSFRESH01"
    assert event.payload_digest == _digest("fresh-payment-failure-body")
    assert event.order_id == order.id
    assert event.case_id == case.id == result.case_id
    assert event.processed_at == NOW.replace(tzinfo=None)
    assert order.external_order_id == "syn_order_PROCESS001"
    assert order.order_number == "DEMO-7001"
    assert order.amount_minor == 18_750
    assert order.currency == "USD"
    assert order.payment_status == "failed"
    assert order.fulfillment_status == "unfulfilled"
    assert case.order_id == order.id
    assert case.source_event_id == event.id
    assert case.rule_key == "payment_failed"
    assert case.case_type == "payment"
    assert case.severity == "high"
    assert case.status == "open"
    assert case.assignee_membership_id is None
    assert case.version == 1
    assert case.due_at == (NOW + timedelta(hours=2)).replace(tzinfo=None)
    assert audit.actor_membership_id is None
    assert audit.action_key == f"webhook:{event.id}:case.created"
    assert audit.action == "case.created_from_webhook"
    assert audit.object_type == "case"
    assert audit.object_id == case.id
    assert audit.object_version == 1
    assert json.loads(audit.changes_json) == {
        "event_id": "evt_PROCESSFRESH01",
        "event_type": "payment.failed",
        "provider": "synthetic",
        "version": 1,
    }


def test_exact_replay_returns_the_same_case_without_consuming_allowance(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    payload = _payload(order_id="syn_order_REPLAY0001", number="DEMO-7002")
    digest = _digest("byte-identical-replay-body")

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        first = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_EXACTREPLAY01",
            payload_digest=digest,
            payload=payload,
            event_limit=1,
            received_at=NOW,
        )

    with factory() as database:
        order_before = database.scalar(
            select(Order).where(Order.organization_id == organization_id)
        )
        assert order_before is not None
        order_updated_at = order_before.updated_at
        replay = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_EXACTREPLAY01",
            payload_digest=digest,
            payload=payload,
            event_limit=1,
            received_at=NOW + timedelta(minutes=5),
        )

    assert replay.event_id == first.event_id
    assert replay.case_id == first.case_id
    assert replay.replayed is True
    with factory() as database:
        replayed_order = database.scalar(
            select(Order).where(Order.organization_id == organization_id)
        )
        assert replayed_order is not None
        assert replayed_order.updated_at == order_updated_at
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


def test_same_event_id_with_different_raw_body_digest_conflicts_without_writes(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    payload = _payload(order_id="syn_order_DIGEST0001", number="DEMO-7003")

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        first = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_DIGESTCHANGE01",
            payload_digest=_digest("{body-without-extra-space}"),
            payload=payload,
            event_limit=20,
            received_at=NOW,
        )

    with factory() as database, pytest.raises(processing.WebhookEventDigestConflict):
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_DIGESTCHANGE01",
            payload_digest=_digest("{body-without-extra-space} "),
            payload=payload,
            event_limit=20,
            received_at=NOW + timedelta(seconds=10),
        )

    with factory() as database:
        case_id = database.scalar(
            select(ExceptionCase.id).where(ExceptionCase.organization_id == organization_id)
        )
        assert case_id == first.case_id
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


def test_two_new_events_reuse_one_matching_order_snapshot(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    payload = _payload(order_id="syn_order_REUSE00001", number="DEMO-7004")

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        first = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_ORDERREUSE001",
            payload_digest=_digest("first-order-event"),
            payload=payload,
            event_limit=20,
            received_at=NOW,
        )
    with factory() as database:
        second = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_ORDERREUSE002",
            payload_digest=_digest("second-order-event"),
            payload=payload,
            event_limit=20,
            received_at=NOW + timedelta(seconds=30),
        )

    assert first.case_id != second.case_id
    assert first.replayed is False
    assert second.replayed is False
    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (2, 1, 2, 2, 2)
        event_ids = set(
            database.scalars(
                select(WebhookEvent.external_event_id).where(
                    WebhookEvent.organization_id == organization_id
                )
            )
        )
        assert event_ids == {"evt_ORDERREUSE001", "evt_ORDERREUSE002"}


@pytest.mark.parametrize("changed_field", ["number", "amount_minor", "currency"])
def test_immutable_order_snapshot_conflict_rolls_back_event_and_allowance(
    app_harness: AppHarness,
    changed_field: str,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    original = _payload(order_id="syn_order_IMMUTABLE1", number="DEMO-7005", amount_minor=500)
    if changed_field == "number":
        changed = _payload(
            order_id="syn_order_IMMUTABLE1",
            number="DEMO-7009",
            amount_minor=500,
        )
    elif changed_field == "amount_minor":
        changed = _payload(
            order_id="syn_order_IMMUTABLE1",
            number="DEMO-7005",
            amount_minor=501,
        )
    else:
        changed = _payload(
            order_id="syn_order_IMMUTABLE1",
            number="DEMO-7005",
            amount_minor=500,
            currency="EUR",
        )

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_IMMUTABLE001",
            payload_digest=_digest("immutable-original"),
            payload=original,
            event_limit=20,
            received_at=NOW,
        )
    with factory() as database, pytest.raises(processing.WebhookOrderSnapshotConflict):
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_IMMUTABLE002",
            payload_digest=_digest("immutable-changed"),
            payload=changed,
            event_limit=20,
            received_at=NOW + timedelta(seconds=10),
        )

    with factory() as database:
        order = database.scalar(select(Order).where(Order.organization_id == organization_id))
        assert order is not None
        assert order.amount_minor == 500
        assert order.order_number == "DEMO-7005"
        assert order.currency == "USD"
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


def test_allowance_exhaustion_rolls_back_the_new_event_claim(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_LIMITFIRST001",
            payload_digest=_digest("limit-first"),
            payload=_payload(order_id="syn_order_LIMIT000001", number="DEMO-7006"),
            event_limit=1,
            received_at=NOW,
        )
    with factory() as database, pytest.raises(processing.DemoWebhookEventLimitExceeded):
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_LIMITSECOND01",
            payload_digest=_digest("limit-second"),
            payload=_payload(order_id="syn_order_LIMIT000002", number="DEMO-7007"),
            event_limit=1,
            received_at=NOW + timedelta(seconds=10),
        )

    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


@pytest.mark.parametrize(
    "failure_stage",
    [
        "event_claimed",
        "allowance_consumed",
        "order_claimed",
        "payment_failed",
        "case_created",
        "audit_appended",
        "event_completed",
    ],
)
def test_failure_at_each_processing_stage_rolls_back_every_business_effect(
    app_harness: AppHarness,
    failure_stage: str,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    class InjectedFailure(Exception):
        pass

    def fail_at_stage(stage: str) -> None:
        if stage == failure_stage:
            raise InjectedFailure

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        with pytest.raises(InjectedFailure):
            processing.process_payment_failed_webhook(
                database,
                organization_id=organization_id,
                integration_id=integration_id,
                external_event_id="evt_FAILURESTAGE1",
                payload_digest=_digest("failure-stage-body"),
                payload=_payload(order_id="syn_order_FAILURE001", number="DEMO-7008"),
                event_limit=20,
                received_at=NOW,
                stage_hook=fail_at_stage,
            )

    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (0, 0, 0, 0, 0)
        retry = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_FAILURESTAGE1",
            payload_digest=_digest("failure-stage-body"),
            payload=_payload(order_id="syn_order_FAILURE001", number="DEMO-7008"),
            event_limit=20,
            received_at=NOW + timedelta(seconds=5),
        )

    assert retry.replayed is False
    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


def test_same_provider_identifiers_are_isolated_between_tenants(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    payload = _payload(order_id="syn_order_TENANT0001", number="DEMO-7010")

    with factory() as database:
        first_organization_id, first_integration_id = _seed_target(
            database,
            name="First tenant",
        )
        second_organization_id, second_integration_id = _seed_target(
            database,
            name="Second tenant",
        )

    results = []
    for organization_id, integration_id in (
        (first_organization_id, first_integration_id),
        (second_organization_id, second_integration_id),
    ):
        with factory() as database:
            results.append(
                processing.process_payment_failed_webhook(
                    database,
                    organization_id=organization_id,
                    integration_id=integration_id,
                    external_event_id="evt_TENANTSHARED1",
                    payload_digest=_digest("same-tenant-scoped-body"),
                    payload=payload,
                    event_limit=20,
                    received_at=NOW,
                )
            )

    assert results[0].case_id != results[1].case_id
    with factory() as database:
        assert _counts(database, organization_id=first_organization_id) == (1, 1, 1, 1, 1)
        assert _counts(database, organization_id=second_organization_id) == (1, 1, 1, 1, 1)
        assert set(database.scalars(select(Order.organization_id))) == {
            first_organization_id,
            second_organization_id,
        }


def test_existing_matching_order_preserves_fulfillment_state(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        existing_order_id = str(uuid4())
        database.add(
            Order(
                id=existing_order_id,
                organization_id=organization_id,
                external_order_id="syn_order_EXISTING01",
                order_number="DEMO-7011",
                amount_minor=7_500,
                currency="USD",
                payment_status="paid",
                fulfillment_status="fulfilled",
                created_at=NOW - timedelta(hours=1),
                updated_at=NOW - timedelta(hours=1),
            )
        )
        database.commit()

    with factory() as database:
        result = processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_EXISTINGORDER1",
            payload_digest=_digest("existing-order-body"),
            payload=_payload(
                order_id="syn_order_EXISTING01",
                number="DEMO-7011",
                amount_minor=7_500,
            ),
            event_limit=20,
            received_at=NOW,
        )

    with factory() as database:
        order = database.get(Order, existing_order_id)
        event = database.scalar(
            select(WebhookEvent).where(WebhookEvent.organization_id == organization_id)
        )
        assert order is not None
        assert event is not None
        assert order.payment_status == "failed"
        assert order.fulfillment_status == "fulfilled"
        assert event.order_id == existing_order_id
        assert event.case_id == result.case_id
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


def test_committed_incomplete_inbox_is_never_misclassified_as_replay(
    app_harness: AppHarness,
) -> None:
    events = import_module("app.repositories.webhook_events")
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    digest = _digest("incomplete-inbox-body")
    payload = _payload(order_id="syn_order_INCOMPLETE1", number="DEMO-7012")

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        events.claim_webhook_event(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_INCOMPLETE001",
            event_type="payment.failed",
            payload_digest=digest,
            occurred_at=payload.occurred_at,
            received_at=NOW,
        )
        database.commit()  # Deliberately simulate a violated historical invariant.

    with factory() as database, pytest.raises(processing.WebhookEventInvariantViolation):
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_INCOMPLETE001",
            payload_digest=digest,
            payload=payload,
            event_limit=20,
            received_at=NOW + timedelta(seconds=5),
        )

    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (1, 0, 0, 0, 0)


def test_webhook_persistence_schema_is_a_safe_metadata_allowlist() -> None:
    assert set(WebhookEvent.__table__.columns.keys()) == {
        "id",
        "organization_id",
        "integration_id",
        "external_event_id",
        "event_type",
        "payload_digest",
        "occurred_at",
        "order_id",
        "case_id",
        "received_at",
        "processed_at",
    }
    forbidden_fragments = {
        "body",
        "signature",
        "header",
        "secret",
        "token",
        "source_ip",
        "email",
        "address",
        "customer",
        "card",
    }
    persisted_names = {
        column.name
        for model in (WebhookEvent, Order, ExceptionCase, AuditEvent)
        for column in model.__table__.columns
    }
    assert all(
        fragment not in persisted_name
        for persisted_name in persisted_names
        for fragment in forbidden_fragments
    )


@pytest.mark.parametrize("target_state", ["non_demo", "expired", "disabled"])
def test_processor_rejects_an_inactive_demo_target_without_writes(
    app_harness: AppHarness,
    target_state: str,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        organization = database.get(Organization, organization_id)
        assert organization is not None
        if target_state == "non_demo":
            organization.is_demo = False
        elif target_state == "expired":
            organization.expires_at = NOW
        else:
            integration = database.get(WebhookIntegration, integration_id)
            assert integration is not None
            integration.enabled = False
        database.commit()

    with factory() as database, pytest.raises(processing.WebhookProcessingTargetUnavailable):
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_UNAVAILABLE01",
            payload_digest=_digest("unavailable-target"),
            payload=_payload(order_id="syn_order_UNAVAILABLE1", number="DEMO-7013"),
            event_limit=20,
            received_at=NOW,
        )

    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (0, 0, 0, 0, 0)


def test_processor_rejects_an_integration_from_another_tenant_without_writes(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        first_organization_id, _first_integration_id = _seed_target(
            database,
            name="First target tenant",
        )
        second_organization_id, second_integration_id = _seed_target(
            database,
            name="Second target tenant",
        )

    with factory() as database, pytest.raises(processing.WebhookProcessingTargetUnavailable):
        processing.process_payment_failed_webhook(
            database,
            organization_id=first_organization_id,
            integration_id=second_integration_id,
            external_event_id="evt_CROSSTENANT01",
            payload_digest=_digest("cross-tenant-target"),
            payload=_payload(order_id="syn_order_CROSSTENANT1", number="DEMO-7016"),
            event_limit=20,
            received_at=NOW,
        )

    with factory() as database:
        assert _counts(database, organization_id=first_organization_id) == (0, 0, 0, 0, 0)
        assert _counts(database, organization_id=second_organization_id) == (0, 0, 0, 0, 0)


def test_fresh_processing_calls_commit_exactly_once(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)

    with factory() as database:
        organization_id, integration_id = _seed_target(database)
        original_commit = database.commit
        commit_count = 0

        def count_commit() -> None:
            nonlocal commit_count
            commit_count += 1
            original_commit()

        monkeypatch.setattr(database, "commit", count_commit)
        processing.process_payment_failed_webhook(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_ONECOMMIT0001",
            payload_digest=_digest("one-commit-body"),
            payload=_payload(order_id="syn_order_ONECOMMIT01", number="DEMO-7014"),
            event_limit=20,
            received_at=NOW,
        )

    assert commit_count == 1


def test_concurrent_sqlite_exact_deliveries_create_one_effect(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    with factory() as database:
        organization_id, integration_id = _seed_target(database)

    start = Barrier(2)

    def deliver() -> WebhookProcessingResult:
        with factory() as database:
            start.wait(timeout=10)
            result = processing.process_payment_failed_webhook(
                database,
                organization_id=organization_id,
                integration_id=integration_id,
                external_event_id="evt_SQLITEEXACT01",
                payload_digest=_digest("sqlite-exact-body"),
                payload=_payload(order_id="syn_order_SQLITE00001", number="DEMO-7015"),
                event_limit=20,
                received_at=NOW,
            )
            return cast(WebhookProcessingResult, result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]

    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].case_id == results[1].case_id
    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)


def test_concurrent_sqlite_events_cannot_overshoot_the_last_allowance_slot(
    app_harness: AppHarness,
) -> None:
    processing = import_module("app.services.webhook_processing")
    factory = cast(sessionmaker[Session], app_harness.app.state.session_factory)
    with factory() as database:
        organization_id, integration_id = _seed_target(database)

    start = Barrier(2)

    def deliver(request_number: int) -> str:
        with factory() as database:
            start.wait(timeout=10)
            try:
                processing.process_payment_failed_webhook(
                    database,
                    organization_id=organization_id,
                    integration_id=integration_id,
                    external_event_id=f"evt_SQLITELIMIT{request_number:02d}",
                    payload_digest=_digest(f"sqlite-limit-body-{request_number}"),
                    payload=_payload(
                        order_id=f"syn_order_SQLITELIMIT{request_number:02d}",
                        number=f"DEMO-{7020 + request_number}",
                    ),
                    event_limit=1,
                    received_at=NOW,
                )
            except processing.DemoWebhookEventLimitExceeded:
                return "limited"
            return "processed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=15)
            for future in [executor.submit(deliver, request_number) for request_number in range(2)]
        ]

    assert sorted(outcomes) == ["limited", "processed"]
    with factory() as database:
        assert _counts(database, organization_id=organization_id) == (1, 1, 1, 1, 1)
