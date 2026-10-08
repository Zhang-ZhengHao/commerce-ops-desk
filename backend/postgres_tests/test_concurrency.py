"""Concurrent command contracts against the real PostgreSQL boundary."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.session import SESSION_COOKIE_NAME, AuthContext
from app.services.case_commands import (
    NOTE_COMMAND,
    _replay_after_integrity_contention,
)
from app.services.demo_workspaces import GLOBAL_CAPACITY_LOCK_DIGEST

if TYPE_CHECKING:
    from postgres_tests.conftest import PostgresAppHarness

SAME_ORIGIN = "http://commerceops.test"


def _bootstrap(
    client: TestClient,
    *,
    key: str,
    role: str = "manager",
) -> Any:
    return client.post(
        "/api/demo/workspaces",
        headers={"Origin": SAME_ORIGIN, "Idempotency-Key": key},
        json={"initial_role": role},
    )


def _command_headers(csrf_token: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": idempotency_key,
    }


def _reset_headers(csrf_token: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
    }


def _send_two(send: Callable[[int], Any]) -> list[Any]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send, request_number) for request_number in range(2)]
        return [future.result(timeout=15) for future in futures]


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

    raise AssertionError(f"PostgreSQL backend {blocked_pid} did not block on backend {blocker_pid}")


def _creation_counts(harness: PostgresAppHarness) -> tuple[int, int, int, int]:
    with Session(harness.engine) as database:
        organization_count = database.scalar(text("SELECT count(*) FROM organizations"))
        session_count = database.scalar(text("SELECT count(*) FROM sessions"))
        receipt_count = database.scalar(text("SELECT count(*) FROM bootstrap_receipts"))
        source_limit_count = database.scalar(
            text(
                """
                SELECT COALESCE(sum(count), 0)
                FROM rate_limits
                WHERE source_digest != :capacity_digest
                """
            ),
            {"capacity_digest": GLOBAL_CAPACITY_LOCK_DIGEST},
        )

    assert organization_count is not None
    assert session_count is not None
    assert receipt_count is not None
    assert source_limit_count is not None
    return (
        int(organization_count),
        int(session_count),
        int(receipt_count),
        int(source_limit_count),
    )


def _synchronize_initial_case_receipt_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make both requests observe an unused command key before either writes."""

    from app.services import case_commands

    original_find = case_commands.find_command_receipt  # type: ignore[attr-defined]
    empty_read_barrier = Barrier(2)
    counter_lock = Lock()
    empty_read_count = 0

    def synchronized_find(*args: Any, **kwargs: Any) -> Any:
        nonlocal empty_read_count
        receipt = original_find(*args, **kwargs)
        if receipt is not None:
            return receipt

        with counter_lock:
            empty_read_count += 1
            should_wait = empty_read_count <= 2
        if should_wait:
            empty_read_barrier.wait(timeout=10)
        return None

    monkeypatch.setattr(case_commands, "find_command_receipt", synchronized_find)


def test_concurrent_exact_bootstrap_retry_creates_and_counts_once(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    start = Barrier(2)

    def send(_request_number: int) -> Any:
        with harness.client(
            source_ip="198.51.100.95",
            raise_server_exceptions=False,
        ) as client:
            start.wait(timeout=10)
            return _bootstrap(client, key="pg-concurrent-bootstrap-retry")

    responses = _send_two(send)

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert responses[0].cookies[SESSION_COOKIE_NAME] == responses[1].cookies[SESSION_COOKIE_NAME]
    assert _creation_counts(harness) == (1, 1, 1, 1)


def test_concurrent_changed_bootstrap_payload_has_one_conflict(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    start = Barrier(2)
    roles = ("manager", "agent")

    def send(request_number: int) -> Any:
        with harness.client(
            source_ip="198.51.100.96",
            raise_server_exceptions=False,
        ) as client:
            start.wait(timeout=10)
            return _bootstrap(
                client,
                key="pg-concurrent-bootstrap-changed",
                role=roles[request_number],
            )

    responses = _send_two(send)

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"] == "Idempotency key is already bound to another payload"
    assert _creation_counts(harness) == (1, 1, 1, 1)


def test_concurrent_exact_case_note_replays_one_effect(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    with harness.client(source_ip="198.51.100.97") as setup_client:
        bootstrap = _bootstrap(setup_client, key="pg-case-exact-bootstrap")
        assert bootstrap.status_code == 201
        target_response = setup_client.get(
            "/api/cases",
            params={"rule_key": "refund_review", "status": "open"},
        )
        assert target_response.status_code == 200
        target = target_response.json()["items"][0]

    organization_id = bootstrap.json()["workspace"]["id"]
    session_cookie = bootstrap.cookies[SESSION_COOKIE_NAME]
    csrf_token = bootstrap.json()["csrf_token"]
    idempotency_key = "pg-concurrent-exact-note"
    note_body = "One PostgreSQL concurrent note."
    _synchronize_initial_case_receipt_reads(monkeypatch)

    def send(request_number: int) -> Any:
        with harness.client(
            source_ip=f"198.51.100.{request_number + 110}",
            raise_server_exceptions=False,
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, session_cookie)
            return client.post(
                f"/api/cases/{target['id']}/notes",
                headers=_command_headers(csrf_token, idempotency_key),
                json={"body": note_body, "version": target["version"]},
            )

    responses = _send_two(send)

    expected = {"case_id": target["id"], "version": target["version"] + 1}
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [expected, expected]

    with Session(harness.engine) as database:
        final_state = database.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM command_receipts
                     WHERE organization_id = :organization_id
                       AND command_type = 'case.note'
                       AND idempotency_key = :idempotency_key),
                    (SELECT count(*) FROM case_notes
                     WHERE organization_id = :organization_id
                       AND case_id = :case_id
                       AND body = :note_body),
                    (SELECT count(*) FROM audit_events
                     WHERE organization_id = :organization_id
                       AND object_id = :case_id
                       AND action = 'case.note_added'),
                    (SELECT version FROM exception_cases
                     WHERE organization_id = :organization_id AND id = :case_id),
                    (SELECT case_note_count FROM organizations
                     WHERE id = :organization_id)
                """
            ),
            {
                "organization_id": organization_id,
                "idempotency_key": idempotency_key,
                "case_id": target["id"],
                "note_body": note_body,
            },
        ).one()

    assert tuple(final_state) == (1, 1, 1, target["version"] + 1, 1)


def test_concurrent_changed_case_payload_has_one_success_and_one_conflict(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_app_harness_factory()
    with harness.client(source_ip="198.51.100.117") as setup_client:
        bootstrap = _bootstrap(setup_client, key="pg-case-changed-bootstrap")
        assert bootstrap.status_code == 201
        cases_response = setup_client.get("/api/cases", params={"status": "open"})
        assert cases_response.status_code == 200
        targets = cases_response.json()["items"][:2]
        assert len(targets) == 2

    organization_id = bootstrap.json()["workspace"]["id"]
    session_cookie = bootstrap.cookies[SESSION_COOKIE_NAME]
    csrf_token = bootstrap.json()["csrf_token"]
    idempotency_key = "pg-concurrent-changed-note"
    note_bodies = ("PostgreSQL changed contender A.", "PostgreSQL changed contender B.")
    _synchronize_initial_case_receipt_reads(monkeypatch)

    def send(request_number: int) -> Any:
        target = targets[request_number]
        with harness.client(
            source_ip=f"198.51.100.{request_number + 118}",
            raise_server_exceptions=False,
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, session_cookie)
            return client.post(
                f"/api/cases/{target['id']}/notes",
                headers=_command_headers(csrf_token, idempotency_key),
                json={
                    "body": note_bodies[request_number],
                    "version": target["version"],
                },
            )

    responses = _send_two(send)

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"] == "Idempotency key is already bound to another payload"
    winning_index = next(
        index for index, response in enumerate(responses) if response.status_code == 200
    )

    with Session(harness.engine) as database:
        final_state = database.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM command_receipts
                     WHERE organization_id = :organization_id
                       AND command_type = 'case.note'
                       AND idempotency_key = :idempotency_key),
                    (SELECT count(*) FROM case_notes
                     WHERE organization_id = :organization_id
                       AND case_id IN (:first_case_id, :second_case_id)),
                    (SELECT count(*) FROM audit_events
                     WHERE organization_id = :organization_id
                       AND action = 'case.note_added'
                       AND object_id IN (:first_case_id, :second_case_id)),
                    (SELECT count(*) FROM exception_cases
                     WHERE organization_id = :organization_id
                       AND id IN (:first_case_id, :second_case_id)
                       AND version = 2),
                    (SELECT case_note_count FROM organizations
                     WHERE id = :organization_id),
                    (SELECT case_id FROM case_notes
                     WHERE organization_id = :organization_id
                       AND case_id IN (:first_case_id, :second_case_id)),
                    (SELECT body FROM case_notes
                     WHERE organization_id = :organization_id
                       AND case_id IN (:first_case_id, :second_case_id))
                """
            ),
            {
                "organization_id": organization_id,
                "idempotency_key": idempotency_key,
                "first_case_id": targets[0]["id"],
                "second_case_id": targets[1]["id"],
            },
        ).one()

    assert tuple(final_state) == (
        1,
        1,
        1,
        1,
        1,
        targets[winning_index]["id"],
        note_bodies[winning_index],
    )


def test_concurrent_different_keys_on_one_case_version_have_one_winner(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    with harness.client(source_ip="198.51.100.98") as setup_client:
        bootstrap = _bootstrap(setup_client, key="pg-case-version-bootstrap")
        assert bootstrap.status_code == 201
        target_response = setup_client.get(
            "/api/cases",
            params={"rule_key": "refund_review", "status": "open"},
        )
        assert target_response.status_code == 200
        target = target_response.json()["items"][0]

    organization_id = bootstrap.json()["workspace"]["id"]
    session_cookie = bootstrap.cookies[SESSION_COOKIE_NAME]
    csrf_token = bootstrap.json()["csrf_token"]
    idempotency_keys = ("pg-case-version-a", "pg-case-version-b")
    note_bodies = ("PostgreSQL contender A.", "PostgreSQL contender B.")
    _synchronize_initial_case_receipt_reads(monkeypatch)

    def send(request_number: int) -> Any:
        with harness.client(
            source_ip=f"198.51.100.{request_number + 120}",
            raise_server_exceptions=False,
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, session_cookie)
            return client.post(
                f"/api/cases/{target['id']}/notes",
                headers=_command_headers(
                    csrf_token,
                    idempotency_keys[request_number],
                ),
                json={
                    "body": note_bodies[request_number],
                    "version": target["version"],
                },
            )

    responses = _send_two(send)

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"] == "Case version conflict"
    winning_index = next(
        index for index, response in enumerate(responses) if response.status_code == 200
    )

    with Session(harness.engine) as database:
        final_state = database.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM command_receipts
                     WHERE organization_id = :organization_id
                       AND command_type = 'case.note'),
                    (SELECT count(*) FROM case_notes
                     WHERE organization_id = :organization_id AND case_id = :case_id),
                    (SELECT count(*) FROM audit_events
                     WHERE organization_id = :organization_id
                       AND object_id = :case_id
                       AND action = 'case.note_added'),
                    (SELECT version FROM exception_cases
                     WHERE organization_id = :organization_id AND id = :case_id),
                    (SELECT case_note_count FROM organizations
                     WHERE id = :organization_id),
                    (SELECT idempotency_key FROM command_receipts
                     WHERE organization_id = :organization_id
                       AND command_type = 'case.note'),
                    (SELECT body FROM case_notes
                     WHERE organization_id = :organization_id AND case_id = :case_id)
                """
            ),
            {"organization_id": organization_id, "case_id": target["id"]},
        ).one()

    assert tuple(final_state) == (
        1,
        1,
        1,
        target["version"] + 1,
        1,
        idempotency_keys[winning_index],
        note_bodies[winning_index],
    )


def test_concurrent_reset_from_one_old_session_creates_one_replacement(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import demo as demo_module

    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    with harness.client(source_ip="198.51.100.99") as bootstrap_client:
        bootstrap = _bootstrap(bootstrap_client, key="pg-reset-race-bootstrap")
    assert bootstrap.status_code == 201
    old_workspace_id = bootstrap.json()["workspace"]["id"]
    old_session_cookie = bootstrap.cookies[SESSION_COOKIE_NAME]
    old_csrf_token = bootstrap.json()["csrf_token"]

    demo_api: Any = demo_module
    original_require_csrf = demo_api.require_csrf
    ready_to_delete = Barrier(2)

    def synchronized_require_csrf(*args: Any, **kwargs: Any) -> None:
        original_require_csrf(*args, **kwargs)
        ready_to_delete.wait(timeout=10)

    monkeypatch.setattr(demo_api, "require_csrf", synchronized_require_csrf)

    def send(_request_number: int) -> Any:
        with harness.client(
            source_ip="198.51.100.99",
            raise_server_exceptions=False,
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, old_session_cookie)
            return client.post(
                "/api/demo/reset",
                headers=_reset_headers(old_csrf_token),
            )

    responses = _send_two(send)

    assert sorted(response.status_code for response in responses) == [201, 401]
    winner = next(response for response in responses if response.status_code == 201)
    winner_workspace_id = winner.json()["workspace"]["id"]
    assert winner_workspace_id != old_workspace_id

    with Session(harness.engine) as database:
        organization_ids = list(database.scalars(text("SELECT id FROM organizations ORDER BY id")))
        old_workspace_rows = database.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM organizations WHERE id = :old_workspace_id),
                    (SELECT count(*) FROM users
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM memberships
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM sessions
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM orders
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM exception_cases
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM case_notes
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM audit_events
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM command_receipts
                     WHERE organization_id = :old_workspace_id),
                    (SELECT count(*) FROM bootstrap_receipts
                     WHERE organization_id = :old_workspace_id)
                """
            ),
            {"old_workspace_id": old_workspace_id},
        ).one()
        source_limit_count = database.scalar(
            text(
                """
                SELECT count
                FROM rate_limits
                WHERE source_digest != :capacity_digest
                """
            ),
            {"capacity_digest": GLOBAL_CAPACITY_LOCK_DIGEST},
        )

    assert organization_ids == [winner_workspace_id]
    assert tuple(old_workspace_rows) == (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert source_limit_count == 2


@pytest.mark.parametrize("operation", ["assignment", "resolution"])
def test_reset_and_in_flight_case_write_complete_without_a_deadlock(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from app.services import case_commands

    harness = postgres_app_harness_factory()
    assert harness.engine.dialect.name == "postgresql"
    with harness.client(source_ip="198.51.100.101") as setup_client:
        bootstrap = _bootstrap(setup_client, key=f"pg-{operation}-reset-bootstrap")
        assert bootstrap.status_code == 201
        target_response = setup_client.get(
            "/api/cases",
            params={"rule_key": "refund_review", "status": "open"},
        )
        assert target_response.status_code == 200
        target = target_response.json()["items"][0]
        agents_response = setup_client.get("/api/agents")
        assert agents_response.status_code == 200
        agent_id = agents_response.json()["items"][0]["membership_id"]

    old_workspace_id = bootstrap.json()["workspace"]["id"]
    old_session_cookie = bootstrap.cookies[SESSION_COOKIE_NAME]
    old_csrf_token = bootstrap.json()["csrf_token"]
    if operation == "assignment":
        command_path = f"/api/cases/{target['id']}/assignment"
        command_payload = {"assignee_id": agent_id, "version": target["version"]}
    else:
        command_path = f"/api/cases/{target['id']}/resolution"
        command_payload = {"reason": "refund_approved", "version": target["version"]}

    original_append_audit_event = case_commands.append_audit_event  # type: ignore[attr-defined]
    case_reached_audit = Event()
    release_case_audit = Event()
    reset_delete_started = Event()
    pid_lock = Lock()
    backend_pids: dict[str, int] = {}

    def pause_before_case_audit(database: Session, *args: Any, **kwargs: Any) -> Any:
        case_pid = database.scalar(text("SELECT pg_backend_pid()"))
        assert case_pid is not None
        with pid_lock:
            backend_pids["case"] = int(case_pid)
        case_reached_audit.set()
        if not release_case_audit.wait(timeout=10):
            raise AssertionError("Timed out before releasing the case audit write")
        return original_append_audit_event(database, *args, **kwargs)

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

    monkeypatch.setattr(case_commands, "append_audit_event", pause_before_case_audit)
    event.listen(harness.engine, "before_cursor_execute", capture_reset_backend)

    def send_case_command() -> Any:
        with harness.client(
            source_ip="198.51.100.102",
            raise_server_exceptions=False,
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, old_session_cookie)
            return client.post(
                command_path,
                headers=_command_headers(
                    old_csrf_token,
                    f"pg-{operation}-during-reset",
                ),
                json=command_payload,
            )

    def send_reset() -> Any:
        with harness.client(
            source_ip="198.51.100.101",
            raise_server_exceptions=False,
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, old_session_cookie)
            return client.post(
                "/api/demo/reset",
                headers=_reset_headers(old_csrf_token),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            case_future = executor.submit(send_case_command)
            reset_future = None
            try:
                assert case_reached_audit.wait(timeout=10)
                reset_future = executor.submit(send_reset)
                assert reset_delete_started.wait(timeout=10)
                with pid_lock:
                    case_pid = backend_pids["case"]
                    reset_pid = backend_pids["reset"]
                _wait_until_backend_is_blocked_by(
                    harness,
                    blocked_pid=reset_pid,
                    blocker_pid=case_pid,
                )
            finally:
                release_case_audit.set()

            case_response = case_future.result(timeout=15)
            assert reset_future is not None
            reset_response = reset_future.result(timeout=15)
    finally:
        release_case_audit.set()
        event.remove(harness.engine, "before_cursor_execute", capture_reset_backend)

    assert case_response.status_code == 200, case_response.text
    assert case_response.json() == {
        "case_id": target["id"],
        "version": target["version"] + 1,
    }
    assert reset_response.status_code == 201, reset_response.text
    replacement_workspace_id = reset_response.json()["workspace"]["id"]
    assert replacement_workspace_id != old_workspace_id

    with Session(harness.engine) as database:
        organization_ids = list(database.scalars(text("SELECT id FROM organizations")))
        old_workspace_count = database.scalar(
            text("SELECT count(*) FROM organizations WHERE id = :old_workspace_id"),
            {"old_workspace_id": old_workspace_id},
        )

    assert organization_ids == [replacement_workspace_id]
    assert old_workspace_count == 0


def test_non_contention_integrity_error_is_never_classified_as_a_replay(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory()
    with harness.client(source_ip="198.51.100.100") as client:
        bootstrap = _bootstrap(client, key="pg-integrity-classification-bootstrap")
        assert bootstrap.status_code == 201
        target_response = client.get(
            "/api/cases",
            params={"rule_key": "refund_review", "status": "open"},
        )
        assert target_response.status_code == 200
        target = target_response.json()["items"][0]
        note = client.post(
            f"/api/cases/{target['id']}/notes",
            headers=_command_headers(
                bootstrap.json()["csrf_token"],
                "pg-integrity-classification-note",
            ),
            json={
                "body": "Create a legitimate receipt before fault injection.",
                "version": target["version"],
            },
        )
        assert note.status_code == 200

    with Session(harness.engine) as database:
        receipt = database.execute(
            text(
                """
                SELECT membership_id, payload_digest
                FROM command_receipts
                WHERE command_type = :command_type
                  AND idempotency_key = :idempotency_key
                """
            ),
            {
                "command_type": NOTE_COMMAND,
                "idempotency_key": "pg-integrity-classification-note",
            },
        ).one()
        with pytest.raises(IntegrityError) as captured:
            database.execute(
                text(
                    """
                    UPDATE exception_cases
                    SET version = 0
                    WHERE id = :case_id
                    """
                ),
                {"case_id": target["id"]},
            )

        fake_context = cast(
            AuthContext,
            SimpleNamespace(membership=SimpleNamespace(id=receipt.membership_id)),
        )
        with pytest.raises(IntegrityError) as reraised:
            _replay_after_integrity_contention(
                database,
                context=fake_context,
                command_type=NOTE_COMMAND,
                idempotency_key="pg-integrity-classification-note",
                payload_digest=receipt.payload_digest,
                error=captured.value,
            )

    assert reraised.value is captured.value
    diagnostic = getattr(reraised.value.orig, "diag", None)
    assert diagnostic is not None
    assert diagnostic.constraint_name == "ck_exception_cases_positive_version"
