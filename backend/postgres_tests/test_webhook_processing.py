"""PostgreSQL concurrency proofs for the one-commit webhook processor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.session import SESSION_COOKIE_NAME
from app.domain.webhook_event import SyntheticWebhookEventPayload
from app.models import (
    AuditEvent,
    ExceptionCase,
    Order,
    Organization,
    WebhookEvent,
    WebhookIntegration,
)
from app.services.webhook_processing import (
    DemoWebhookEventLimitExceeded,
    WebhookEventDigestConflict,
    WebhookProcessingResult,
    process_payment_failed_webhook,
)

if TYPE_CHECKING:
    from postgres_tests.conftest import PostgresAppHarness

NOW = datetime(2026, 10, 7, 12, 30, 0, tzinfo=UTC)
SAME_ORIGIN = "http://commerceops.test"


@dataclass(frozen=True)
class _DeliveryOutcome:
    index: int
    state: Literal["processed", "digest_conflict", "quota_limited", "rolled_back"]
    result: WebhookProcessingResult | None = None


class _SameIdentityGate:
    """Pause the first event claimant while its unique insert is uncommitted."""

    def __init__(self) -> None:
        self.start = Barrier(2)
        self.first_claimed = Event()
        self.release_first = Event()
        self._lock = Lock()
        self.pids: dict[int, int] = {}
        self.first_index: int | None = None

    def prepare(self, database: Session, *, index: int) -> None:
        backend_pid = database.scalar(text("SELECT pg_backend_pid()"))
        assert backend_pid is not None
        with self._lock:
            self.pids[index] = int(backend_pid)
        self.start.wait(timeout=10)

    def pause_first_claim(self, *, index: int, stage: str) -> bool:
        if stage != "event_claimed":
            return False

        with self._lock:
            is_first = self.first_index is None
            if is_first:
                self.first_index = index
        if is_first:
            self.first_claimed.set()
            if not self.release_first.wait(timeout=10):
                raise AssertionError("Timed out before releasing the first webhook claimant")
        return is_first

    def wait_for_blocking_pair(self) -> tuple[int, int, int, int]:
        if not self.first_claimed.wait(timeout=10):
            raise AssertionError("No webhook claimant reached the synchronization hook")
        with self._lock:
            first_index = self.first_index
            pids = dict(self.pids)
        assert first_index is not None
        assert set(pids) == {0, 1}
        waiting_index = 1 - first_index
        return first_index, pids[first_index], waiting_index, pids[waiting_index]


def _wait_until_backend_is_blocked_by(
    harness: PostgresAppHarness,
    *,
    blocked_pid: int,
    blocker_pid: int,
) -> None:
    deadline = monotonic() + 10
    with Session(harness.engine) as observer:
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

    raise AssertionError(f"PostgreSQL backend {blocked_pid} did not block on {blocker_pid}")


def _seed_target(harness: PostgresAppHarness) -> tuple[str, str]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    with Session(harness.engine) as database:
        database.add(
            Organization(
                id=organization_id,
                name="PostgreSQL webhook processing test",
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
    order_id: str,
    number: str,
    amount_minor: int = 18_750,
    currency: str = "USD",
) -> SyntheticWebhookEventPayload:
    return SyntheticWebhookEventPayload.model_validate(
        {
            "type": "payment.failed",
            "occurred_at": "2026-10-07T12:29:00Z",
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


def _assert_single_complete_effect(
    harness: PostgresAppHarness,
    *,
    organization_id: str,
    integration_id: str,
    external_event_id: str,
    payload_digest: str,
    payload: SyntheticWebhookEventPayload,
    expected_case_id: str,
) -> None:
    with Session(harness.engine) as database:
        organization = database.get(Organization, organization_id)
        events = list(
            database.scalars(
                select(WebhookEvent).where(WebhookEvent.organization_id == organization_id)
            )
        )
        orders = list(
            database.scalars(select(Order).where(Order.organization_id == organization_id))
        )
        cases = list(
            database.scalars(
                select(ExceptionCase).where(ExceptionCase.organization_id == organization_id)
            )
        )
        audits = list(
            database.scalars(
                select(AuditEvent).where(AuditEvent.organization_id == organization_id)
            )
        )

    assert organization is not None
    assert organization.webhook_event_count == 1
    assert len(events) == len(orders) == len(cases) == len(audits) == 1
    inbox_event = events[0]
    order = orders[0]
    case = cases[0]
    audit = audits[0]
    submitted_order = payload.data.order

    assert inbox_event.integration_id == integration_id
    assert inbox_event.external_event_id == external_event_id
    assert inbox_event.event_type == "payment.failed"
    assert inbox_event.payload_digest == payload_digest
    assert inbox_event.order_id == order.id
    assert inbox_event.case_id == case.id == expected_case_id
    assert inbox_event.processed_at is not None

    assert order.external_order_id == submitted_order.id
    assert order.order_number == submitted_order.number
    assert order.amount_minor == submitted_order.amount_minor
    assert order.currency == submitted_order.currency
    assert order.payment_status == "failed"
    assert order.fulfillment_status == "unfulfilled"

    assert case.organization_id == organization_id
    assert case.order_id == order.id
    assert case.source_event_id == inbox_event.id
    assert case.rule_key == "payment_failed"
    assert case.status == "open"
    assert case.version == 1

    assert audit.organization_id == organization_id
    assert audit.actor_membership_id is None
    assert audit.action_key == f"webhook:{inbox_event.id}:case.created"
    assert audit.action == "case.created_from_webhook"
    assert audit.object_id == case.id
    assert audit.object_version == 1
    assert json.loads(audit.changes_json) == {
        "event_id": external_event_id,
        "event_type": "payment.failed",
        "provider": "synthetic",
        "version": 1,
    }


def test_concurrent_exact_deliveries_replay_one_committed_effect(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    organization_id, integration_id = _seed_target(harness)
    external_event_id = "evt_PGEXACT0001"
    payload_digest = _digest("postgresql-exact-webhook-body")
    payload = _payload(order_id="syn_order_PGEXACT001", number="DEMO-8101")
    gate = _SameIdentityGate()

    def deliver(index: int) -> _DeliveryOutcome:
        with Session(harness.engine) as database:
            gate.prepare(database, index=index)

            def pause_first(stage: str) -> None:
                gate.pause_first_claim(index=index, stage=stage)

            result = process_payment_failed_webhook(
                database,
                organization_id=organization_id,
                integration_id=integration_id,
                integration_key_version=1,
                external_event_id=external_event_id,
                payload_digest=payload_digest,
                payload=payload,
                event_limit=20,
                received_at=NOW + timedelta(seconds=index),
                stage_hook=pause_first,
            )
            return _DeliveryOutcome(index=index, state="processed", result=result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, index) for index in range(2)]
        try:
            first_index, first_pid, waiting_index, waiting_pid = gate.wait_for_blocking_pair()
            _wait_until_backend_is_blocked_by(
                harness,
                blocked_pid=waiting_pid,
                blocker_pid=first_pid,
            )
        finally:
            gate.release_first.set()
        outcomes = [future.result(timeout=20) for future in futures]

    by_index = {outcome.index: outcome for outcome in outcomes}
    first_result = by_index[first_index].result
    waiting_result = by_index[waiting_index].result
    assert first_result is not None
    assert waiting_result is not None
    assert first_result.replayed is False
    assert waiting_result.replayed is True
    assert first_result.event_id == waiting_result.event_id == external_event_id
    assert first_result.case_id == waiting_result.case_id
    _assert_single_complete_effect(
        harness,
        organization_id=organization_id,
        integration_id=integration_id,
        external_event_id=external_event_id,
        payload_digest=payload_digest,
        payload=payload,
        expected_case_id=first_result.case_id,
    )


def test_concurrent_same_event_id_with_different_digests_has_one_conflict(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    organization_id, integration_id = _seed_target(harness)
    external_event_id = "evt_PGDIGEST001"
    payloads = (
        _payload(order_id="syn_order_PGDIGESTA1", number="DEMO-8111"),
        _payload(order_id="syn_order_PGDIGESTB1", number="DEMO-8112"),
    )
    payload_digests = (
        _digest("postgresql-event-body-a"),
        _digest("postgresql-event-body-b"),
    )
    gate = _SameIdentityGate()

    def deliver(index: int) -> _DeliveryOutcome:
        with Session(harness.engine) as database:
            gate.prepare(database, index=index)

            def pause_first(stage: str) -> None:
                gate.pause_first_claim(index=index, stage=stage)

            try:
                result = process_payment_failed_webhook(
                    database,
                    organization_id=organization_id,
                    integration_id=integration_id,
                    integration_key_version=1,
                    external_event_id=external_event_id,
                    payload_digest=payload_digests[index],
                    payload=payloads[index],
                    event_limit=20,
                    received_at=NOW + timedelta(seconds=index),
                    stage_hook=pause_first,
                )
            except WebhookEventDigestConflict:
                return _DeliveryOutcome(index=index, state="digest_conflict")
            return _DeliveryOutcome(index=index, state="processed", result=result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, index) for index in range(2)]
        try:
            first_index, first_pid, waiting_index, waiting_pid = gate.wait_for_blocking_pair()
            _wait_until_backend_is_blocked_by(
                harness,
                blocked_pid=waiting_pid,
                blocker_pid=first_pid,
            )
        finally:
            gate.release_first.set()
        outcomes = [future.result(timeout=20) for future in futures]

    by_index = {outcome.index: outcome for outcome in outcomes}
    assert by_index[first_index].state == "processed"
    assert by_index[waiting_index].state == "digest_conflict"
    winning_result = by_index[first_index].result
    assert winning_result is not None
    assert winning_result.replayed is False
    _assert_single_complete_effect(
        harness,
        organization_id=organization_id,
        integration_id=integration_id,
        external_event_id=external_event_id,
        payload_digest=payload_digests[first_index],
        payload=payloads[first_index],
        expected_case_id=winning_result.case_id,
    )


def test_concurrent_new_events_contend_for_one_remaining_allowance_slot(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    organization_id, integration_id = _seed_target(harness)
    external_event_ids = ("evt_PGQUOTAA001", "evt_PGQUOTAB001")
    payload_digests = (_digest("quota-body-a"), _digest("quota-body-b"))
    payloads = (
        _payload(order_id="syn_order_PGQUOTAA01", number="DEMO-8121"),
        _payload(order_id="syn_order_PGQUOTAB01", number="DEMO-8122"),
    )
    gate = _SameIdentityGate()

    def deliver(index: int) -> _DeliveryOutcome:
        with Session(harness.engine) as database:
            gate.prepare(database, index=index)

            def pause_first(stage: str) -> None:
                gate.pause_first_claim(index=index, stage=stage)

            try:
                result = process_payment_failed_webhook(
                    database,
                    organization_id=organization_id,
                    integration_id=integration_id,
                    integration_key_version=1,
                    external_event_id=external_event_ids[index],
                    payload_digest=payload_digests[index],
                    payload=payloads[index],
                    event_limit=1,
                    received_at=NOW + timedelta(seconds=index),
                    stage_hook=pause_first,
                )
            except DemoWebhookEventLimitExceeded:
                return _DeliveryOutcome(index=index, state="quota_limited")
            return _DeliveryOutcome(index=index, state="processed", result=result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, index) for index in range(2)]
        try:
            first_index, first_pid, waiting_index, waiting_pid = gate.wait_for_blocking_pair()
            _wait_until_backend_is_blocked_by(
                harness,
                blocked_pid=waiting_pid,
                blocker_pid=first_pid,
            )
        finally:
            gate.release_first.set()
        outcomes = [future.result(timeout=20) for future in futures]

    by_index = {outcome.index: outcome for outcome in outcomes}
    assert by_index[first_index].state == "processed"
    assert by_index[waiting_index].state == "quota_limited"
    winner = by_index[first_index]
    loser = by_index[waiting_index]
    assert winner.result is not None
    assert winner.result.replayed is False
    assert winner.index != loser.index
    _assert_single_complete_effect(
        harness,
        organization_id=organization_id,
        integration_id=integration_id,
        external_event_id=external_event_ids[winner.index],
        payload_digest=payload_digests[winner.index],
        payload=payloads[winner.index],
        expected_case_id=winner.result.case_id,
    )


def test_waiting_delivery_takes_over_after_uncommitted_winner_rolls_back(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    organization_id, integration_id = _seed_target(harness)
    external_event_id = "evt_PGROLLBACK01"
    payload_digest = _digest("postgresql-rollback-takeover-body")
    payload = _payload(order_id="syn_order_PGROLLBACK1", number="DEMO-8131")
    gate = _SameIdentityGate()

    class InjectedWinnerRollback(Exception):
        pass

    def deliver(index: int) -> _DeliveryOutcome:
        with Session(harness.engine) as database:
            gate.prepare(database, index=index)

            def fail_first_claim(stage: str) -> None:
                if gate.pause_first_claim(index=index, stage=stage):
                    raise InjectedWinnerRollback

            try:
                result = process_payment_failed_webhook(
                    database,
                    organization_id=organization_id,
                    integration_id=integration_id,
                    integration_key_version=1,
                    external_event_id=external_event_id,
                    payload_digest=payload_digest,
                    payload=payload,
                    event_limit=20,
                    received_at=NOW + timedelta(seconds=index),
                    stage_hook=fail_first_claim,
                )
            except InjectedWinnerRollback:
                return _DeliveryOutcome(index=index, state="rolled_back")
            return _DeliveryOutcome(index=index, state="processed", result=result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, index) for index in range(2)]
        try:
            first_index, first_pid, waiting_index, waiting_pid = gate.wait_for_blocking_pair()
            _wait_until_backend_is_blocked_by(
                harness,
                blocked_pid=waiting_pid,
                blocker_pid=first_pid,
            )
        finally:
            gate.release_first.set()
        outcomes = [future.result(timeout=20) for future in futures]

    by_index = {outcome.index: outcome for outcome in outcomes}
    assert by_index[first_index].state == "rolled_back"
    assert by_index[first_index].result is None
    assert by_index[waiting_index].state == "processed"
    takeover_result = by_index[waiting_index].result
    assert takeover_result is not None
    assert takeover_result.replayed is False
    _assert_single_complete_effect(
        harness,
        organization_id=organization_id,
        integration_id=integration_id,
        external_event_id=external_event_id,
        payload_digest=payload_digest,
        payload=payload,
        expected_case_id=takeover_result.case_id,
    )


def test_parent_delete_reset_waits_for_webhook_processor_without_deadlock(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    with harness.client(source_ip="198.51.100.151") as setup_client:
        bootstrap = setup_client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "pg-webhook-reset-bootstrap",
            },
            json={"initial_role": "manager"},
        )
    assert bootstrap.status_code == 201
    old_organization_id = bootstrap.json()["workspace"]["id"]
    old_session_cookie = bootstrap.cookies[SESSION_COOKIE_NAME]
    old_csrf_token = bootstrap.json()["csrf_token"]
    with Session(harness.engine) as database:
        integration_id = database.scalar(
            select(WebhookIntegration.id).where(
                WebhookIntegration.organization_id == old_organization_id
            )
        )
    assert integration_id is not None

    payload = _payload(order_id="syn_order_PGRESET001", number="DEMO-8141")
    payload_digest = _digest("postgresql-reset-lock-order-body")
    processor_paused = Event()
    release_processor = Event()
    reset_delete_started = Event()
    pid_lock = Lock()
    backend_pids: dict[str, int] = {}

    def capture_reset_backend(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized_statement = " ".join(statement.lower().split())
        if not normalized_statement.startswith("delete from organizations"):
            return
        driver_connection: Any = connection.connection.driver_connection
        with pid_lock:
            backend_pids["reset"] = int(driver_connection.info.backend_pid)
        reset_delete_started.set()

    sqlalchemy_event.listen(harness.engine, "before_cursor_execute", capture_reset_backend)

    def process_delivery() -> WebhookProcessingResult:
        with Session(harness.engine) as database:
            backend_pid = database.scalar(text("SELECT pg_backend_pid()"))
            assert backend_pid is not None
            with pid_lock:
                backend_pids["processor"] = int(backend_pid)

            def pause_after_audit(stage: str) -> None:
                if stage != "audit_appended":
                    return
                processor_paused.set()
                if not release_processor.wait(timeout=10):
                    raise AssertionError("Timed out before releasing webhook processing")

            return process_payment_failed_webhook(
                database,
                organization_id=old_organization_id,
                integration_id=integration_id,
                integration_key_version=1,
                external_event_id="evt_PGRESET0001",
                payload_digest=payload_digest,
                payload=payload,
                event_limit=20,
                received_at=NOW,
                stage_hook=pause_after_audit,
            )

    def reset_workspace() -> Any:
        with harness.client(
            source_ip="198.51.100.151",
            raise_server_exceptions=False,
        ) as reset_client:
            reset_client.cookies.set(SESSION_COOKIE_NAME, old_session_cookie)
            return reset_client.post(
                "/api/demo/reset",
                headers={
                    "Origin": SAME_ORIGIN,
                    "X-CSRF-Token": old_csrf_token,
                },
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            processor_future = executor.submit(process_delivery)
            reset_future = None
            try:
                assert processor_paused.wait(timeout=10)
                reset_future = executor.submit(reset_workspace)
                assert reset_delete_started.wait(timeout=10)
                with pid_lock:
                    processor_pid = backend_pids["processor"]
                    reset_pid = backend_pids["reset"]
                _wait_until_backend_is_blocked_by(
                    harness,
                    blocked_pid=reset_pid,
                    blocker_pid=processor_pid,
                )
            finally:
                release_processor.set()

            processing_result = processor_future.result(timeout=20)
            assert reset_future is not None
            reset_response = reset_future.result(timeout=20)
    finally:
        release_processor.set()
        sqlalchemy_event.remove(harness.engine, "before_cursor_execute", capture_reset_backend)

    assert processing_result.event_id == "evt_PGRESET0001"
    assert processing_result.replayed is False
    assert reset_response.status_code == 201, reset_response.text
    replacement_organization_id = reset_response.json()["workspace"]["id"]
    assert replacement_organization_id != old_organization_id

    with Session(harness.engine) as database:
        old_state = database.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM organizations WHERE id = :organization_id),
                    (SELECT count(*) FROM webhook_integrations
                     WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM webhook_events
                     WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM orders WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM exception_cases
                     WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM audit_events
                     WHERE organization_id = :organization_id)
                """
            ),
            {"organization_id": old_organization_id},
        ).one()
        replacement_state = database.execute(
            text(
                """
                SELECT
                    (SELECT webhook_event_count FROM organizations
                     WHERE id = :organization_id),
                    (SELECT count(*) FROM webhook_integrations
                     WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM webhook_events
                     WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM orders WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM exception_cases
                     WHERE organization_id = :organization_id),
                    (SELECT count(*) FROM audit_events
                     WHERE organization_id = :organization_id)
                """
            ),
            {"organization_id": replacement_organization_id},
        ).one()

    assert tuple(old_state) == (0, 0, 0, 0, 0, 0)
    assert tuple(replacement_state) == (0, 1, 0, 4, 4, 0)
