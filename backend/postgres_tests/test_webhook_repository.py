"""PostgreSQL concurrency proofs for webhook persistence primitives."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Order, Organization, WebhookEvent, WebhookIntegration
from app.repositories import webhook_events, webhook_orders

if TYPE_CHECKING:
    from postgres_tests.conftest import PostgresAppHarness

NOW = datetime(2026, 10, 8, 10, 30, 0, tzinfo=UTC)
PAYLOAD_DIGEST = "a" * 64


@dataclass
class _BackendProbe:
    ready: Event = field(default_factory=Event)
    pid: int | None = None

    def publish(self, pid: int) -> None:
        self.pid = pid
        self.ready.set()

    def wait(self) -> int:
        assert self.ready.wait(timeout=10), "Timed out waiting for the contender backend"
        assert self.pid is not None
        return self.pid


@dataclass(frozen=True)
class _EventClaimResult:
    event_id: str
    inserted: bool
    payload_digest: str


@dataclass(frozen=True)
class _OrderClaimResult:
    order_id: str
    inserted: bool


def _backend_pid(database: Session) -> int:
    pid = database.scalar(text("SELECT pg_backend_pid()"))
    assert pid is not None
    return int(pid)


def _wait_until_backend_is_blocked_by(
    engine: Engine,
    *,
    blocked_pid: int,
    blocker_pid: int,
) -> None:
    deadline = monotonic() + 10
    with Session(engine) as observer:
        while monotonic() < deadline:
            activity = observer.execute(
                text(
                    """
                    SELECT wait_event_type, pg_blocking_pids(pid) AS blocking_pids
                    FROM pg_stat_activity
                    WHERE pid = :blocked_pid
                    """
                ),
                {"blocked_pid": blocked_pid},
            ).one_or_none()
            if (
                activity is not None
                and activity.wait_event_type == "Lock"
                and blocker_pid in activity.blocking_pids
            ):
                return
            observer.rollback()

    raise AssertionError(f"PostgreSQL backend {blocked_pid} did not block on backend {blocker_pid}")


def _seed_target(engine: Engine) -> tuple[str, str]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    with Session(engine) as database:
        database.add(
            Organization(
                id=organization_id,
                name="PostgreSQL webhook repository workspace",
                is_demo=True,
                case_note_count=0,
                webhook_event_count=0,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=4),
            )
        )
        database.flush()
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


def _claim_event_in_contender(
    engine: Engine,
    probe: _BackendProbe,
    *,
    organization_id: str,
    integration_id: str,
    external_event_id: str,
) -> _EventClaimResult:
    with Session(engine) as database:
        probe.publish(_backend_pid(database))
        claim = webhook_events.claim_webhook_event(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id=external_event_id,
            event_type="payment.failed",
            payload_digest=PAYLOAD_DIGEST,
            occurred_at=NOW - timedelta(minutes=2),
            received_at=NOW,
        )
        result = _EventClaimResult(
            event_id=claim.event.id,
            inserted=claim.inserted,
            payload_digest=claim.event.payload_digest,
        )
        database.commit()
        return result


def _claim_order_in_contender(
    engine: Engine,
    probe: _BackendProbe,
    *,
    organization_id: str,
    external_order_id: str,
) -> _OrderClaimResult:
    with Session(engine) as database:
        probe.publish(_backend_pid(database))
        claim = webhook_orders.get_or_create_webhook_order(
            database,
            organization_id=organization_id,
            external_order_id=external_order_id,
            order_number="DEMO-9101",
            amount_minor=12_900,
            currency="USD",
            now=NOW,
        )
        result = _OrderClaimResult(order_id=claim.order.id, inserted=claim.inserted)
        database.commit()
        return result


def test_event_claim_waits_for_commit_and_reads_the_committed_winner(
    postgres_app_harness: PostgresAppHarness,
) -> None:
    engine = postgres_app_harness.engine
    organization_id, integration_id = _seed_target(engine)
    external_event_id = "evt_PGCOMMIT01"

    with Session(engine) as winner:
        winner_pid = _backend_pid(winner)
        winner_claim = webhook_events.claim_webhook_event(
            winner,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id=external_event_id,
            event_type="payment.failed",
            payload_digest=PAYLOAD_DIGEST,
            occurred_at=NOW - timedelta(minutes=2),
            received_at=NOW,
        )
        assert winner_claim.inserted is True
        winner_event_id = winner_claim.event.id

        probe = _BackendProbe()
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(
                _claim_event_in_contender,
                engine,
                probe,
                organization_id=organization_id,
                integration_id=integration_id,
                external_event_id=external_event_id,
            )
            try:
                contender_pid = probe.wait()
                _wait_until_backend_is_blocked_by(
                    engine,
                    blocked_pid=contender_pid,
                    blocker_pid=winner_pid,
                )
                winner.commit()
                contender_result = contender.result(timeout=15)
            finally:
                if winner.in_transaction():
                    winner.rollback()

    assert contender_result == _EventClaimResult(
        event_id=winner_event_id,
        inserted=False,
        payload_digest=PAYLOAD_DIGEST,
    )
    with Session(engine) as database:
        events = database.scalars(select(WebhookEvent)).all()
    assert [event.id for event in events] == [winner_event_id]


def test_event_claim_waits_for_rollback_and_becomes_the_inserted_winner(
    postgres_app_harness: PostgresAppHarness,
) -> None:
    engine = postgres_app_harness.engine
    organization_id, integration_id = _seed_target(engine)
    external_event_id = "evt_PGROLLBACK1"

    with Session(engine) as rolled_back_winner:
        winner_pid = _backend_pid(rolled_back_winner)
        first_claim = webhook_events.claim_webhook_event(
            rolled_back_winner,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id=external_event_id,
            event_type="payment.failed",
            payload_digest=PAYLOAD_DIGEST,
            occurred_at=NOW - timedelta(minutes=2),
            received_at=NOW,
        )
        assert first_claim.inserted is True
        rolled_back_event_id = first_claim.event.id

        probe = _BackendProbe()
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(
                _claim_event_in_contender,
                engine,
                probe,
                organization_id=organization_id,
                integration_id=integration_id,
                external_event_id=external_event_id,
            )
            try:
                contender_pid = probe.wait()
                _wait_until_backend_is_blocked_by(
                    engine,
                    blocked_pid=contender_pid,
                    blocker_pid=winner_pid,
                )
                rolled_back_winner.rollback()
                contender_result = contender.result(timeout=15)
            finally:
                if rolled_back_winner.in_transaction():
                    rolled_back_winner.rollback()

    assert contender_result.inserted is True
    assert contender_result.event_id != rolled_back_event_id
    assert contender_result.payload_digest == PAYLOAD_DIGEST
    with Session(engine) as database:
        events = database.scalars(select(WebhookEvent)).all()
    assert [event.id for event in events] == [contender_result.event_id]


def test_order_upsert_waits_for_commit_and_creates_only_one_order(
    postgres_app_harness: PostgresAppHarness,
) -> None:
    engine = postgres_app_harness.engine
    organization_id, _integration_id = _seed_target(engine)
    external_order_id = "syn_order_PGREPORACE01"

    with Session(engine) as winner:
        winner_pid = _backend_pid(winner)
        winner_claim = webhook_orders.get_or_create_webhook_order(
            winner,
            organization_id=organization_id,
            external_order_id=external_order_id,
            order_number="DEMO-9101",
            amount_minor=12_900,
            currency="USD",
            now=NOW,
        )
        assert winner_claim.inserted is True
        winner_order_id = winner_claim.order.id

        probe = _BackendProbe()
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(
                _claim_order_in_contender,
                engine,
                probe,
                organization_id=organization_id,
                external_order_id=external_order_id,
            )
            try:
                contender_pid = probe.wait()
                _wait_until_backend_is_blocked_by(
                    engine,
                    blocked_pid=contender_pid,
                    blocker_pid=winner_pid,
                )
                winner.commit()
                contender_result = contender.result(timeout=15)
            finally:
                if winner.in_transaction():
                    winner.rollback()

    assert contender_result == _OrderClaimResult(
        order_id=winner_order_id,
        inserted=False,
    )
    with Session(engine) as database:
        order_count = database.scalar(
            select(func.count(Order.id)).where(
                Order.organization_id == organization_id,
                Order.external_order_id == external_order_id,
            )
        )
    assert order_count == 1


def test_event_conflict_target_does_not_swallow_a_primary_key_violation(
    postgres_app_harness: PostgresAppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = postgres_app_harness.engine
    organization_id, integration_id = _seed_target(engine)
    with Session(engine) as database:
        existing = webhook_events.claim_webhook_event(
            database,
            organization_id=organization_id,
            integration_id=integration_id,
            external_event_id="evt_PGPRIMARY01",
            event_type="payment.failed",
            payload_digest=PAYLOAD_DIGEST,
            occurred_at=NOW - timedelta(minutes=2),
            received_at=NOW,
        )
        database.commit()
        existing_event_id = existing.event.id

    duplicate_id = UUID(existing_event_id)

    def duplicate_uuid() -> UUID:
        return duplicate_id

    monkeypatch.setattr(webhook_events, "uuid4", duplicate_uuid)
    with Session(engine) as database:
        with pytest.raises(IntegrityError) as raised:
            webhook_events.claim_webhook_event(
                database,
                organization_id=organization_id,
                integration_id=integration_id,
                external_event_id="evt_PGPRIMARY02",
                event_type="payment.failed",
                payload_digest=PAYLOAD_DIGEST,
                occurred_at=NOW - timedelta(minutes=1),
                received_at=NOW,
            )
        database.rollback()

    diagnostic = getattr(raised.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == "webhook_events_pkey"
    with Session(engine) as database:
        assert database.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_order_conflict_target_does_not_swallow_a_primary_key_violation(
    postgres_app_harness: PostgresAppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = postgres_app_harness.engine
    organization_id, _integration_id = _seed_target(engine)
    with Session(engine) as database:
        existing = webhook_orders.get_or_create_webhook_order(
            database,
            organization_id=organization_id,
            external_order_id="syn_order_PGPRIMARY01",
            order_number="DEMO-9201",
            amount_minor=9_900,
            currency="USD",
            now=NOW,
        )
        database.commit()
        existing_order_id = existing.order.id

    duplicate_id = UUID(existing_order_id)

    def duplicate_uuid() -> UUID:
        return duplicate_id

    monkeypatch.setattr(webhook_orders, "uuid4", duplicate_uuid)
    with Session(engine) as database:
        with pytest.raises(IntegrityError) as raised:
            webhook_orders.get_or_create_webhook_order(
                database,
                organization_id=organization_id,
                external_order_id="syn_order_PGPRIMARY02",
                order_number="DEMO-9202",
                amount_minor=10_900,
                currency="USD",
                now=NOW,
            )
        database.rollback()

    diagnostic = getattr(raised.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == "orders_pkey"
    with Session(engine) as database:
        assert database.scalar(select(func.count()).select_from(Order)) == 1
