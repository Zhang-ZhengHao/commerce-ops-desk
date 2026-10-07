"""Role-scoped audit history read model."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.auth.session import AuthContext, as_utc
from app.repositories.audit import AuditRow, list_audit_events
from app.repositories.cases import find_case


def _event_payload(row: AuditRow) -> dict[str, Any]:
    actor = None
    if row.actor is not None and row.actor_user is not None:
        actor = {
            "membership_id": row.actor.id,
            "display_name": row.actor_user.display_name,
        }
    changes = json.loads(row.event.changes_json)
    if not isinstance(changes, dict):
        raise RuntimeError("audit event changes must be an object")
    return {
        "id": row.event.id,
        "action": row.event.action,
        "object_type": row.event.object_type,
        "object_id": row.event.object_id,
        "actor": actor,
        "changes": changes,
        "created_at": as_utc(row.event.created_at),
    }


def audit_history_payload(
    db: Session,
    *,
    context: AuthContext,
    case_id: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any] | None:
    if case_id is not None and find_case(db, context=context, case_id=case_id) is None:
        return None
    rows, total = list_audit_events(
        db,
        context=context,
        case_id=case_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [_event_payload(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
