"""Read-only authentication target lookup contracts for webhook ingress."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import timedelta
from typing import NoReturn, cast
from uuid import uuid4

import pytest
from conftest import AppHarness
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Organization, WebhookIntegration
from app.repositories.webhook_integrations import (
    ActiveDemoWebhookTarget,
    find_active_demo_webhook_target,
)


def _session_factory(harness: AppHarness) -> sessionmaker[Session]:
    return cast(sessionmaker[Session], harness.app.state.session_factory)


def _seed_target(
    database: Session,
    harness: AppHarness,
    *,
    enabled: bool = True,
    is_demo: bool = True,
    expires_in: timedelta = timedelta(hours=4),
) -> tuple[str, str]:
    organization_id = str(uuid4())
    integration_id = str(uuid4())
    now = harness.clock()
    database.add(
        Organization(
            id=organization_id,
            name="Webhook target repository workspace",
            is_demo=is_demo,
            case_note_count=0,
            webhook_event_count=0,
            created_at=now,
            expires_at=now + expires_in,
        )
    )
    database.add(
        WebhookIntegration(
            id=integration_id,
            organization_id=organization_id,
            provider="synthetic",
            key_version=7,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
    )
    database.commit()
    return organization_id, integration_id


def test_lookup_returns_a_minimal_frozen_target_with_one_join_query_and_no_transaction_control(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory(app_harness)

    with factory() as database:
        organization_id, integration_id = _seed_target(database, app_harness)
        statements: list[str] = []
        bind = cast(Engine, database.get_bind())

        def record_statement(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        def reject_transaction_control() -> NoReturn:
            raise AssertionError("target lookup must not control the transaction")

        monkeypatch.setattr(database, "commit", reject_transaction_control)
        monkeypatch.setattr(database, "rollback", reject_transaction_control)
        event.listen(bind, "before_cursor_execute", record_statement)
        try:
            target = find_active_demo_webhook_target(
                database,
                integration_id=integration_id,
                now=app_harness.clock(),
            )
        finally:
            event.remove(bind, "before_cursor_execute", record_statement)

    assert target == ActiveDemoWebhookTarget(
        organization_id=organization_id,
        integration_id=integration_id,
        key_version=7,
    )
    assert [field.name for field in fields(target)] == [
        "organization_id",
        "integration_id",
        "key_version",
    ]
    with pytest.raises(FrozenInstanceError):
        setattr(target, fields(target)[0].name, "replacement-organization")
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert " JOIN " in statements[0].upper()


@pytest.mark.parametrize(
    ("seed_target", "enabled", "is_demo", "expires_in"),
    [
        pytest.param(False, True, True, timedelta(hours=4), id="unknown"),
        pytest.param(True, False, True, timedelta(hours=4), id="disabled"),
        pytest.param(True, True, False, timedelta(hours=4), id="non-demo"),
        pytest.param(True, True, True, timedelta(seconds=-1), id="expired"),
        pytest.param(True, True, True, timedelta(0), id="expires-at-now"),
    ],
)
def test_lookup_hides_every_inactive_target(
    app_harness: AppHarness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_target: bool,
    enabled: bool,
    is_demo: bool,
    expires_in: timedelta,
) -> None:
    factory = _session_factory(app_harness)

    with factory() as database:
        if seed_target:
            _organization_id, integration_id = _seed_target(
                database,
                app_harness,
                enabled=enabled,
                is_demo=is_demo,
                expires_in=expires_in,
            )
        else:
            integration_id = str(uuid4())

        def reject_transaction_control() -> NoReturn:
            raise AssertionError("target lookup must not control the transaction")

        monkeypatch.setattr(database, "commit", reject_transaction_control)
        monkeypatch.setattr(database, "rollback", reject_transaction_control)

        target = find_active_demo_webhook_target(
            database,
            integration_id=integration_id,
            now=app_harness.clock(),
        )

    assert target is None
