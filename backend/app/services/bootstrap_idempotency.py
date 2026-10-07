"""Transactional claims and safe replay data for demo workspace creation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import BootstrapReceipt
from app.services.idempotent_commands import IdempotencyConflictError


@dataclass(frozen=True)
class StoredBootstrapResult:
    organization_id: str
    result_session_id: str
    response_body: dict[str, Any]


def claim_bootstrap_receipt(
    db: Session,
    *,
    source_digest: str,
    idempotency_key: str,
    payload_digest: str,
    now: datetime,
) -> bool:
    """Claim one source/key pair without committing an incomplete receipt."""
    values = {
        "source_digest": source_digest,
        "idempotency_key": idempotency_key,
        "payload_digest": payload_digest,
        "organization_id": None,
        "result_session_id": None,
        "response_json": None,
        "created_at": now,
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_statement = sqlite_insert(BootstrapReceipt).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_nothing(
            index_elements=[
                BootstrapReceipt.source_digest,
                BootstrapReceipt.idempotency_key,
            ]
        )
        result = db.execute(sqlite_statement)
    elif dialect_name == "postgresql":
        postgresql_statement = postgresql_insert(BootstrapReceipt).values(**values)
        postgresql_statement = postgresql_statement.on_conflict_do_nothing(
            index_elements=[
                BootstrapReceipt.source_digest,
                BootstrapReceipt.idempotency_key,
            ]
        )
        result = db.execute(postgresql_statement)
    else:
        raise RuntimeError("unsupported bootstrap idempotency database")

    cursor_result = cast(CursorResult[Any], result)
    return cursor_result.rowcount == 1


def find_bootstrap_receipt(
    db: Session,
    *,
    source_digest: str,
    idempotency_key: str,
) -> BootstrapReceipt | None:
    return db.scalar(
        select(BootstrapReceipt).where(
            BootstrapReceipt.source_digest == source_digest,
            BootstrapReceipt.idempotency_key == idempotency_key,
        )
    )


def complete_bootstrap_receipt(
    db: Session,
    *,
    source_digest: str,
    idempotency_key: str,
    organization_id: str,
    result_session_id: str,
    response_body: Mapping[str, object],
) -> None:
    safe_response_json = json.dumps(
        response_body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result = cast(
        CursorResult[Any],
        db.execute(
            update(BootstrapReceipt)
            .where(
                BootstrapReceipt.source_digest == source_digest,
                BootstrapReceipt.idempotency_key == idempotency_key,
                BootstrapReceipt.organization_id.is_(None),
                BootstrapReceipt.result_session_id.is_(None),
                BootstrapReceipt.response_json.is_(None),
            )
            .values(
                organization_id=organization_id,
                result_session_id=result_session_id,
                response_json=safe_response_json,
            )
        ),
    )
    if result.rowcount != 1:
        raise RuntimeError("bootstrap receipt claim was lost before completion")


def replay_bootstrap_receipt(
    receipt: BootstrapReceipt,
    *,
    payload_digest: str,
) -> StoredBootstrapResult:
    if receipt.payload_digest != payload_digest:
        raise IdempotencyConflictError
    if (
        receipt.organization_id is None
        or receipt.result_session_id is None
        or receipt.response_json is None
    ):
        raise RuntimeError("bootstrap receipt is incomplete")

    decoded = json.loads(receipt.response_json)
    if not isinstance(decoded, dict):
        raise RuntimeError("bootstrap receipt response is invalid")
    return StoredBootstrapResult(
        organization_id=receipt.organization_id,
        result_session_id=receipt.result_session_id,
        response_body=decoded,
    )
