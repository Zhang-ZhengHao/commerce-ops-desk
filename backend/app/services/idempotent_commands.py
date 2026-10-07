"""Database-backed idempotency receipts for cookie-authenticated commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CommandReceipt


class IdempotencyConflictError(Exception):
    """The command key was already bound to a different canonical payload."""


@dataclass(frozen=True)
class StoredCommandResult:
    response_status: int
    response_body: dict[str, Any]
    result_membership_id: str
    result_session_id: str


def canonical_json_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def find_command_receipt(
    db: Session,
    *,
    membership_id: str,
    command_type: str,
    idempotency_key: str,
) -> CommandReceipt | None:
    return db.scalar(
        select(CommandReceipt).where(
            CommandReceipt.membership_id == membership_id,
            CommandReceipt.command_type == command_type,
            CommandReceipt.idempotency_key == idempotency_key,
        )
    )


def find_command_receipt_by_result_session(
    db: Session,
    *,
    organization_id: str,
    result_session_id: str,
    command_type: str,
    idempotency_key: str,
) -> CommandReceipt | None:
    """Find the command whose exact response produced the current session."""
    return db.scalar(
        select(CommandReceipt).where(
            CommandReceipt.organization_id == organization_id,
            CommandReceipt.result_session_id == result_session_id,
            CommandReceipt.command_type == command_type,
            CommandReceipt.idempotency_key == idempotency_key,
        )
    )


def replay_command(
    receipt: CommandReceipt,
    *,
    payload_digest: str,
) -> StoredCommandResult:
    if receipt.payload_digest != payload_digest:
        raise IdempotencyConflictError
    decoded = json.loads(receipt.response_json)
    if not isinstance(decoded, dict):
        raise RuntimeError("command receipt response is invalid")
    return StoredCommandResult(
        response_status=receipt.response_status,
        response_body=decoded,
        result_membership_id=receipt.result_membership_id,
        result_session_id=receipt.result_session_id,
    )


def store_command_receipt(
    db: Session,
    *,
    organization_id: str,
    membership_id: str,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
    response_status: int,
    response_body: Mapping[str, object],
    result_membership_id: str,
    result_session_id: str,
    now: datetime,
) -> CommandReceipt:
    safe_response_json = json.dumps(
        response_body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt = CommandReceipt(
        id=str(uuid4()),
        organization_id=organization_id,
        membership_id=membership_id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        payload_digest=payload_digest,
        response_status=response_status,
        response_json=safe_response_json,
        result_membership_id=result_membership_id,
        result_session_id=result_session_id,
        created_at=now,
    )
    db.add(receipt)
    db.flush()
    return receipt
