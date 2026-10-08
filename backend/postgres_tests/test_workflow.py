"""Runtime workflow proof against the PostgreSQL application boundary."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from app.services.demo_workspaces import GLOBAL_CAPACITY_LOCK_DIGEST

if TYPE_CHECKING:
    from postgres_tests.conftest import PostgresAppHarness

SAME_ORIGIN = "http://commerceops.test"


def _command_headers(csrf_token: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Origin": SAME_ORIGIN,
        "X-CSRF-Token": csrf_token,
        "Idempotency-Key": idempotency_key,
    }


def test_postgresql_ready_and_manager_to_agent_case_workflow(
    postgres_app_harness: PostgresAppHarness,
) -> None:
    with postgres_app_harness.client() as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "database": "reachable"}

        manager = client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "pg-workflow-bootstrap",
            },
            json={"initial_role": "manager"},
        )
        assert manager.status_code == 201
        manager_payload = manager.json()
        organization_id = manager_payload["workspace"]["id"]

        agents = client.get("/api/agents")
        assert agents.status_code == 200
        agent_id = agents.json()["items"][0]["membership_id"]

        queue = client.get("/api/cases", params={"rule_key": "refund_review"})
        assert queue.status_code == 200
        assert queue.json()["total"] == 1
        target = queue.json()["items"][0]
        assert target["order"]["order_number"] == "DEMO-1043"
        assert target["version"] == 1

        assigned = client.post(
            f"/api/cases/{target['id']}/assignment",
            headers=_command_headers(
                manager_payload["csrf_token"],
                "pg-workflow-assignment",
            ),
            json={"assignee_id": agent_id, "version": 1},
        )
        assert assigned.status_code == 200
        assert assigned.json() == {"case_id": target["id"], "version": 2}

        agent = client.post(
            "/api/demo/role",
            headers=_command_headers(
                manager_payload["csrf_token"],
                "pg-workflow-switch-agent",
            ),
            json={"role": "agent"},
        )
        assert agent.status_code == 200
        assert agent.json()["identity"]["membership_id"] == agent_id

        noted = client.post(
            f"/api/cases/{target['id']}/notes",
            headers=_command_headers(
                agent.json()["csrf_token"],
                "pg-workflow-note",
            ),
            json={"body": "Refund evidence verified in PostgreSQL.", "version": 2},
        )
        assert noted.status_code == 200
        assert noted.json() == {"case_id": target["id"], "version": 3}

        resolved = client.post(
            f"/api/cases/{target['id']}/resolution",
            headers=_command_headers(
                agent.json()["csrf_token"],
                "pg-workflow-resolution",
            ),
            json={"reason": "refund_approved", "version": 3},
        )
        assert resolved.status_code == 200
        assert resolved.json() == {"case_id": target["id"], "version": 4}

    with postgres_app_harness.engine.connect() as connection:
        final_case = connection.execute(
            text(
                """
                SELECT status, assignee_membership_id, resolution_reason, version
                FROM exception_cases
                WHERE organization_id = :organization_id AND id = :case_id
                """
            ),
            {"organization_id": organization_id, "case_id": target["id"]},
        ).one()
        receipts = connection.execute(
            text(
                """
                SELECT command_type, idempotency_key, response_status
                FROM command_receipts
                WHERE organization_id = :organization_id
                ORDER BY command_type
                """
            ),
            {"organization_id": organization_id},
        ).all()
        bootstrap_receipts = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM bootstrap_receipts
                WHERE organization_id = :organization_id
                  AND idempotency_key = 'pg-workflow-bootstrap'
                  AND result_session_id IS NOT NULL
                  AND response_json IS NOT NULL
                """
            ),
            {"organization_id": organization_id},
        )
        audits = connection.execute(
            text(
                """
                SELECT action, object_version
                FROM audit_events
                WHERE organization_id = :organization_id AND object_id = :case_id
                ORDER BY object_version
                """
            ),
            {"organization_id": organization_id, "case_id": target["id"]},
        ).all()
        notes = connection.execute(
            text(
                """
                SELECT author_membership_id, body
                FROM case_notes
                WHERE organization_id = :organization_id AND case_id = :case_id
                """
            ),
            {"organization_id": organization_id, "case_id": target["id"]},
        ).all()

    assert final_case == ("resolved", agent_id, "refund_approved", 4)
    assert receipts == [
        ("case.assignment", "pg-workflow-assignment", 200),
        ("case.note", "pg-workflow-note", 200),
        ("case.resolution", "pg-workflow-resolution", 200),
        ("demo.role.switch", "pg-workflow-switch-agent", 200),
    ]
    assert bootstrap_receipts == 1
    assert audits == [
        ("case.assigned", 2),
        ("case.note_added", 3),
        ("case.resolved", 4),
    ]
    assert notes == [(agent_id, "Refund evidence verified in PostgreSQL.")]


def _bootstrap_workspace(client: Any, *, key: str) -> Any:
    return client.post(
        "/api/demo/workspaces",
        headers={"Origin": SAME_ORIGIN, "Idempotency-Key": key},
        json={"initial_role": "manager"},
    )


def test_postgresql_rate_limit_branch_rejects_the_second_workspace(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory(demo_source_hourly_limit=1)
    assert harness.engine.dialect.name == "postgresql"

    with harness.client(source_ip="198.51.100.71") as client:
        accepted = _bootstrap_workspace(client, key="pg-rate-limit-accepted")
        limited = _bootstrap_workspace(client, key="pg-rate-limit-rejected")

    assert accepted.status_code == 201
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Demo workspace rate limit reached"
    assert int(limited.headers["Retry-After"]) > 0

    with harness.engine.connect() as connection:
        source_counters = connection.execute(
            text(
                """
                SELECT count
                FROM rate_limits
                WHERE count > 0
                """
            )
        ).all()
        organization_count = connection.scalar(text("SELECT count(*) FROM organizations"))

    assert source_counters == [(1,)]
    assert organization_count == 1


def test_postgresql_capacity_lock_allows_only_one_concurrent_workspace(
    postgres_app_harness_factory: Callable[..., PostgresAppHarness],
) -> None:
    harness = postgres_app_harness_factory(
        demo_active_workspace_limit=1,
        demo_source_hourly_limit=10,
    )
    assert harness.engine.dialect.name == "postgresql"
    barrier = Barrier(2)

    def bootstrap_concurrently(index: int) -> int:
        with harness.client(source_ip=f"198.51.100.{80 + index}") as client:
            barrier.wait(timeout=10)
            response = _bootstrap_workspace(client, key=f"pg-capacity-{index}")
            return int(response.status_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(bootstrap_concurrently, range(2)))

    assert statuses == [201, 503]

    with harness.engine.connect() as connection:
        organization_count = connection.scalar(text("SELECT count(*) FROM organizations"))
        capacity_rows = connection.execute(
            text(
                """
                SELECT count
                FROM rate_limits
                WHERE source_digest = :source_digest
                """
            ),
            {"source_digest": GLOBAL_CAPACITY_LOCK_DIGEST},
        ).all()

    assert organization_count == 1
    assert capacity_rows == [(0,)]
