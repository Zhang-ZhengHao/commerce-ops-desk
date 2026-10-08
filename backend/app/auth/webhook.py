"""Pure authentication primitives for the synthetic webhook boundary."""

import hmac
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

WEBHOOK_PATH_PREFIX = b"/api/webhooks/synthetic/"
TIMESTAMP_PATTERN = re.compile(rb"[1-9][0-9]{0,9}", flags=re.ASCII)
EVENT_ID_PATTERN = re.compile(rb"evt_[A-Za-z0-9]{8,64}", flags=re.ASCII)
SIGNATURE_PATTERN = re.compile(rb"v1=[0-9a-f]{64}", flags=re.ASCII)
TIMESTAMP_HEADER = b"x-webhook-timestamp"
EVENT_ID_HEADER = b"x-webhook-event-id"
SIGNATURE_HEADER = b"x-webhook-signature"


@dataclass(frozen=True)
class WebhookAuthHeaders:
    timestamp: int
    event_id: str
    signature: bytes


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def parse_canonical_webhook_path(
    *,
    route_integration_id: str,
    raw_path: bytes | None,
) -> str | None:
    try:
        encoded_id = route_integration_id.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not _is_canonical_uuid(route_integration_id):
        return None
    if raw_path != WEBHOOK_PATH_PREFIX + encoded_id:
        return None
    return route_integration_id


def parse_webhook_auth_headers(
    raw_headers: Sequence[tuple[bytes, bytes]],
) -> WebhookAuthHeaders | None:
    security_values: dict[bytes, list[bytes]] = {
        TIMESTAMP_HEADER: [],
        EVENT_ID_HEADER: [],
        SIGNATURE_HEADER: [],
    }
    for raw_name, raw_value in raw_headers:
        name = raw_name.lower()
        if name not in security_values:
            continue
        if b"\r" in raw_value or b"\n" in raw_value:
            return None
        security_values[name].append(raw_value)

    if any(len(values) != 1 for values in security_values.values()):
        return None
    timestamp = security_values[TIMESTAMP_HEADER][0]
    event_id = security_values[EVENT_ID_HEADER][0]
    signature = security_values[SIGNATURE_HEADER][0]
    if TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        return None
    if EVENT_ID_PATTERN.fullmatch(event_id) is None:
        return None
    if SIGNATURE_PATTERN.fullmatch(signature) is None:
        return None

    return WebhookAuthHeaders(
        timestamp=int(timestamp),
        event_id=event_id.decode("ascii"),
        signature=bytes.fromhex(signature[3:].decode("ascii")),
    )


def is_webhook_timestamp_current(*, timestamp: int, now: datetime) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("webhook clock must be timezone-aware")
    current_timestamp = now.timestamp()
    return current_timestamp - 300 <= timestamp <= current_timestamp + 300


def derive_webhook_integration_key(
    master_secret: bytes,
    *,
    integration_id: str,
    key_version: int,
) -> bytes:
    if len(master_secret) < 32:
        raise ValueError("webhook master secret must contain at least 32 bytes")
    if not _is_canonical_uuid(integration_id):
        raise ValueError("webhook integration id must be a canonical UUID")
    if type(key_version) is not int or key_version < 1:
        raise ValueError("webhook key version must be a positive integer")
    context = (f"commerce-ops:synthetic-webhook-key:v1\n{integration_id}\n{key_version}").encode(
        "ascii"
    )
    return hmac.digest(master_secret, context, "sha256")


def build_webhook_signed_bytes(
    *,
    timestamp: int,
    integration_id: str,
    event_id: str,
    raw_body: bytes,
) -> bytes:
    if type(timestamp) is not int:
        raise ValueError("webhook timestamp must be canonical Unix seconds")
    encoded_timestamp = str(timestamp).encode("ascii")
    if TIMESTAMP_PATTERN.fullmatch(encoded_timestamp) is None:
        raise ValueError("webhook timestamp must be canonical Unix seconds")
    if not _is_canonical_uuid(integration_id):
        raise ValueError("webhook integration id must be a canonical UUID")
    if not event_id.isascii():
        raise ValueError("webhook event id must be canonical ASCII")
    encoded_event_id = event_id.encode("ascii")
    if EVENT_ID_PATTERN.fullmatch(encoded_event_id) is None:
        raise ValueError("webhook event id must be canonical ASCII")
    prefix = (
        b"v1\n"
        + encoded_timestamp
        + b"\n"
        + integration_id.encode("ascii")
        + b"\n"
        + encoded_event_id
        + b"\n"
    )
    return prefix + raw_body


def sign_webhook_request(
    *,
    integration_key: bytes,
    timestamp: int,
    integration_id: str,
    event_id: str,
    raw_body: bytes,
) -> str:
    signed_bytes = build_webhook_signed_bytes(
        timestamp=timestamp,
        integration_id=integration_id,
        event_id=event_id,
        raw_body=raw_body,
    )
    return f"v1={hmac.digest(integration_key, signed_bytes, 'sha256').hex()}"


def verify_webhook_signature(
    *,
    provided_signature: bytes,
    integration_key: bytes,
    timestamp: int,
    integration_id: str,
    event_id: str,
    raw_body: bytes,
) -> bool:
    signed_bytes = build_webhook_signed_bytes(
        timestamp=timestamp,
        integration_id=integration_id,
        event_id=event_id,
        raw_body=raw_body,
    )
    expected_signature = hmac.digest(integration_key, signed_bytes, "sha256")
    return hmac.compare_digest(provided_signature, expected_signature)
