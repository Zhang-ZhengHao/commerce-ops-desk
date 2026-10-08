"""Server-owned webhook integration lifecycle contracts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import NoReturn, cast
from uuid import UUID

import pytest
from conftest import SAME_ORIGIN, AppHarness
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Organization, WebhookIntegration
from app.repositories.webhook_integrations import (
    WebhookIntegrationRequiresDemoOrganization,
    create_demo_webhook_integration,
)


def _session_factory(harness: AppHarness) -> sessionmaker[Session]:
    return cast(sessionmaker[Session], harness.app.state.session_factory)


def _organization(harness: AppHarness, *, is_demo: bool) -> Organization:
    now = harness.clock()
    return Organization(
        id="11111111-1111-4111-8111-111111111111",
        name="Synthetic workspace",
        is_demo=is_demo,
        created_at=now,
        expires_at=now,
    )


def test_repository_creates_one_synthetic_uuid4_without_owning_the_transaction(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory(app_harness)
    now = app_harness.clock()

    with factory() as database:
        organization = _organization(app_harness, is_demo=True)
        database.add(organization)
        database.flush()

        def reject_transaction_control() -> NoReturn:
            raise AssertionError("repository must not control the transaction")

        monkeypatch.setattr(database, "commit", reject_transaction_control)
        monkeypatch.setattr(database, "rollback", reject_transaction_control)

        integration = create_demo_webhook_integration(
            database,
            organization=organization,
            now=now,
        )

        parsed_id = UUID(integration.id)
        assert str(parsed_id) == integration.id
        assert parsed_id.version == 4
        assert integration.organization_id == organization.id
        assert integration.provider == "synthetic"
        assert integration.key_version == 1
        assert integration.enabled is True
        assert integration.created_at == now
        assert integration.updated_at == now
        assert database.scalar(select(func.count(WebhookIntegration.id))) == 1


def test_repository_rejects_a_non_demo_organization_without_staging_an_integration(
    app_harness: AppHarness,
) -> None:
    factory = _session_factory(app_harness)

    with factory() as database:
        organization = _organization(app_harness, is_demo=False)
        database.add(organization)
        database.flush()

        with pytest.raises(WebhookIntegrationRequiresDemoOrganization):
            create_demo_webhook_integration(
                database,
                organization=organization,
                now=app_harness.clock(),
            )

        assert database.scalar(select(func.count(WebhookIntegration.id))) == 0


def test_bootstrap_creates_one_integration_even_when_ingress_is_disabled(
    app_harness_factory: Callable[..., AppHarness],
) -> None:
    harness = app_harness_factory(webhook_enabled=False)
    assert harness.settings.webhook_enabled is False

    with harness.client() as client:
        response = client.post(
            "/api/demo/workspaces",
            headers={
                "Origin": SAME_ORIGIN,
                "Idempotency-Key": "integration-disabled-bootstrap",
            },
            json={"initial_role": "manager"},
        )

    assert response.status_code == 201
    organization_id = response.json()["workspace"]["id"]
    with sqlite3.connect(harness.database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, provider, key_version, enabled
            FROM webhook_integrations
            WHERE organization_id = ?
            """,
            (organization_id,),
        ).fetchall()
        counter = connection.execute(
            "SELECT webhook_event_count FROM organizations WHERE id = ?",
            (organization_id,),
        ).fetchone()

    assert len(rows) == 1
    parsed_id = UUID(str(rows[0][0]))
    assert str(parsed_id) == rows[0][0]
    assert parsed_id.version == 4
    assert rows[0][1:] == ("synthetic", 1, 1)
    assert counter == (0,)


def test_exact_bootstrap_replay_does_not_duplicate_the_integration(
    app_harness: AppHarness,
) -> None:
    with app_harness.client() as client:
        headers = {
            "Origin": SAME_ORIGIN,
            "Idempotency-Key": "integration-exact-replay",
        }
        first = client.post(
            "/api/demo/workspaces",
            headers=headers,
            json={"initial_role": "agent"},
        )
        assert first.status_code == 201
        organization_id = first.json()["workspace"]["id"]
        with sqlite3.connect(app_harness.database_path) as connection:
            first_integration = connection.execute(
                """
                SELECT id FROM webhook_integrations
                WHERE organization_id = ?
                """,
                (organization_id,),
            ).fetchone()

        replay = client.post(
            "/api/demo/workspaces",
            headers=headers,
            json={"initial_role": "agent"},
        )

    assert replay.status_code == 201
    assert replay.json() == first.json()
    with sqlite3.connect(app_harness.database_path) as connection:
        integrations = connection.execute(
            "SELECT id FROM webhook_integrations ORDER BY id"
        ).fetchall()

    assert first_integration is not None
    assert integrations == [first_integration]
