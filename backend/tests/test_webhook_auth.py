"""Pure HMAC and authentication-material contracts for webhook ingress."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from app.auth.webhook import (
    build_webhook_signed_bytes,
    derive_webhook_integration_key,
    is_webhook_timestamp_current,
    parse_canonical_webhook_path,
    parse_webhook_auth_headers,
    sign_webhook_request,
    verify_webhook_signature,
)

MASTER_SECRET = b"0123456789abcdef0123456789abcdef"
INTEGRATION_ID = "123e4567-e89b-42d3-a456-426614174000"
KEY_VERSION = 7
TIMESTAMP = 1_791_376_496
EVENT_ID = "evt_A1b2C3d4"
RAW_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T12:34:56Z",'
    b'"data":{"order":{"id":"syn_order_A1B2C3D4","number":"DEMO-1045",'
    b'"amount_minor":12900,"currency":"USD"}}}'
)
DERIVED_KEY_HEX = "e3f5de1e6139d7bd34d19ebb007f5ac53cca648e6a959c7a0775465975bcbe42"
SIGNATURE_HEX = "c45a0fab6ffaa66f5421b37fc78c25189ff92ff6c9de34556a35cb360dd5f089"
SIGNATURE = f"v1={SIGNATURE_HEX}"
PATH_PREFIX = b"/api/webhooks/synthetic/"


def _auth_headers(
    *,
    timestamp: bytes = str(TIMESTAMP).encode("ascii"),
    event_id: bytes = EVENT_ID.encode("ascii"),
    signature: bytes = SIGNATURE.encode("ascii"),
) -> list[tuple[bytes, bytes]]:
    return [
        (b"x-webhook-timestamp", timestamp),
        (b"x-webhook-event-id", event_id),
        (b"x-webhook-signature", signature),
    ]


def test_key_derivation_and_signature_match_independent_fixed_vectors() -> None:
    integration_key = derive_webhook_integration_key(
        MASTER_SECRET,
        integration_id=INTEGRATION_ID,
        key_version=KEY_VERSION,
    )

    assert integration_key.hex() == DERIVED_KEY_HEX
    assert (
        sign_webhook_request(
            integration_key=integration_key,
            timestamp=TIMESTAMP,
            integration_id=INTEGRATION_ID,
            event_id=EVENT_ID,
            raw_body=RAW_BODY,
        )
        == SIGNATURE
    )


def test_signed_bytes_append_the_body_without_decoding_or_normalizing_it() -> None:
    raw_body = b' {"type": "payment.failed"}\n\xff'

    signed = build_webhook_signed_bytes(
        timestamp=TIMESTAMP,
        integration_id=INTEGRATION_ID,
        event_id=EVENT_ID,
        raw_body=raw_body,
    )

    assert signed == (
        b"v1\n"
        + str(TIMESTAMP).encode("ascii")
        + b"\n"
        + INTEGRATION_ID.encode("ascii")
        + b"\n"
        + EVENT_ID.encode("ascii")
        + b"\n"
        + raw_body
    )


def test_signature_verification_uses_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def capture_compare_digest(provided: bytes, expected: bytes) -> bool:
        comparisons.append((provided, expected))
        return True

    monkeypatch.setattr("app.auth.webhook.hmac.compare_digest", capture_compare_digest)

    accepted = verify_webhook_signature(
        provided_signature=bytes.fromhex(SIGNATURE_HEX),
        integration_key=bytes.fromhex(DERIVED_KEY_HEX),
        timestamp=TIMESTAMP,
        integration_id=INTEGRATION_ID,
        event_id=EVENT_ID,
        raw_body=RAW_BODY,
    )

    assert accepted is True
    expected_digest = bytes.fromhex(SIGNATURE_HEX)
    assert comparisons == [(expected_digest, expected_digest)]


def test_bad_signature_is_compared_exactly_once_with_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def reject_compare_digest(provided: bytes, expected: bytes) -> bool:
        comparisons.append((provided, expected))
        return False

    monkeypatch.setattr("app.auth.webhook.hmac.compare_digest", reject_compare_digest)
    provided_signature = b"\x00" * 32

    accepted = verify_webhook_signature(
        provided_signature=provided_signature,
        integration_key=bytes.fromhex(DERIVED_KEY_HEX),
        timestamp=TIMESTAMP,
        integration_id=INTEGRATION_ID,
        event_id=EVENT_ID,
        raw_body=RAW_BODY,
    )

    assert accepted is False
    assert comparisons == [(provided_signature, bytes.fromhex(SIGNATURE_HEX))]


def test_one_body_byte_change_invalidates_the_signature() -> None:
    integration_key = bytes.fromhex(DERIVED_KEY_HEX)
    supplied_signature = bytes.fromhex(SIGNATURE_HEX)

    assert verify_webhook_signature(
        provided_signature=supplied_signature,
        integration_key=integration_key,
        timestamp=TIMESTAMP,
        integration_id=INTEGRATION_ID,
        event_id=EVENT_ID,
        raw_body=RAW_BODY,
    )
    for altered_body in (
        RAW_BODY.replace(b"12900", b"12901", 1),
        RAW_BODY + b" ",
        RAW_BODY + b"\n",
    ):
        assert not verify_webhook_signature(
            provided_signature=supplied_signature,
            integration_key=integration_key,
            timestamp=TIMESTAMP,
            integration_id=INTEGRATION_ID,
            event_id=EVENT_ID,
            raw_body=altered_body,
        )


def test_canonical_route_and_raw_path_resolve_the_same_integration() -> None:
    assert (
        parse_canonical_webhook_path(
            route_integration_id=INTEGRATION_ID,
            raw_path=PATH_PREFIX + INTEGRATION_ID.encode("ascii"),
        )
        == INTEGRATION_ID
    )


@pytest.mark.parametrize(
    ("route_integration_id", "raw_path"),
    [
        (INTEGRATION_ID.upper(), PATH_PREFIX + INTEGRATION_ID.upper().encode("ascii")),
        (INTEGRATION_ID.replace("-", ""), PATH_PREFIX + INTEGRATION_ID.replace("-", "").encode()),
        (f"{{{INTEGRATION_ID}}}", PATH_PREFIX + f"{{{INTEGRATION_ID}}}".encode()),
        (f" {INTEGRATION_ID}", PATH_PREFIX + f" {INTEGRATION_ID}".encode()),
        (INTEGRATION_ID, PATH_PREFIX + INTEGRATION_ID.encode() + b"/"),
        (INTEGRATION_ID, PATH_PREFIX + b"%31" + INTEGRATION_ID[1:].encode()),
        (INTEGRATION_ID, b"/api/webhooks/SYNTHETIC/" + INTEGRATION_ID.encode()),
        (INTEGRATION_ID, None),
    ],
)
def test_noncanonical_or_percent_encoded_routes_fail_closed(
    route_integration_id: str,
    raw_path: bytes | None,
) -> None:
    assert (
        parse_canonical_webhook_path(
            route_integration_id=route_integration_id,
            raw_path=raw_path,
        )
        is None
    )


def test_raw_security_headers_parse_without_framework_header_folding() -> None:
    headers = [
        (b"X-Webhook-Timestamp", str(TIMESTAMP).encode("ascii")),
        (b"x-WEBHOOK-event-id", EVENT_ID.encode("ascii")),
        (b"X-WEBHOOK-SIGNATURE", SIGNATURE.encode("ascii")),
        (b"x-unrelated", b"one"),
        (b"x-unrelated", b"two"),
    ]

    parsed = parse_webhook_auth_headers(headers)

    assert parsed is not None
    assert parsed.timestamp == TIMESTAMP
    assert parsed.event_id == EVENT_ID
    assert parsed.signature == bytes.fromhex(SIGNATURE_HEX)


def test_minimum_canonical_timestamp_header_is_accepted() -> None:
    parsed = parse_webhook_auth_headers(_auth_headers(timestamp=b"1"))

    assert parsed is not None
    assert parsed.timestamp == 1


def test_maximum_length_event_id_header_is_accepted() -> None:
    event_id = b"evt_" + b"a" * 64

    parsed = parse_webhook_auth_headers(_auth_headers(event_id=event_id))

    assert parsed is not None
    assert parsed.event_id == event_id.decode("ascii")


@pytest.mark.parametrize(
    "header_name",
    [b"x-webhook-timestamp", b"x-webhook-event-id", b"x-webhook-signature"],
)
def test_each_security_header_is_required_exactly_once(header_name: bytes) -> None:
    headers = _auth_headers()
    matching_header = next(header for header in headers if header[0] == header_name)
    missing = [header for header in headers if header[0] != header_name]
    duplicated = [*headers, matching_header]
    case_variant_duplicate = [
        *headers,
        (header_name.upper(), matching_header[1]),
    ]

    assert parse_webhook_auth_headers(missing) is None
    assert parse_webhook_auth_headers(duplicated) is None
    assert parse_webhook_auth_headers(case_variant_duplicate) is None


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("timestamp", b""),
        ("timestamp", b"0"),
        ("timestamp", b"01"),
        ("timestamp", b"+1"),
        ("timestamp", b"-1"),
        ("timestamp", b" 1"),
        ("timestamp", b"1 "),
        ("timestamp", b"10000000000"),
        ("timestamp", b"\xff"),
        ("event_id", b"evt_1234567"),
        ("event_id", b"evt_" + b"a" * 65),
        ("event_id", b"evt_1234_678"),
        ("event_id", b"evt_1234-678"),
        ("event_id", b"live_12345678"),
        ("event_id", b"evt_1234567\xff"),
        ("signature", b"v1=" + b"a" * 63),
        ("signature", b"v1=" + b"a" * 65),
        ("signature", b"v1=" + b"A" * 64),
        ("signature", b"v2=" + b"a" * 64),
        ("signature", b"v1=" + b"g" * 64),
        ("signature", b"v1=" + b"a" * 64 + b" "),
    ],
)
def test_auth_header_grammars_reject_noncanonical_values(
    field: str,
    invalid_value: bytes,
) -> None:
    values = {
        "timestamp": str(TIMESTAMP).encode("ascii"),
        "event_id": EVENT_ID.encode("ascii"),
        "signature": SIGNATURE.encode("ascii"),
    }
    values[field] = invalid_value

    assert (
        parse_webhook_auth_headers(
            _auth_headers(
                timestamp=values["timestamp"],
                event_id=values["event_id"],
                signature=values["signature"],
            )
        )
        is None
    )


@pytest.mark.parametrize("field", ["timestamp", "event_id", "signature"])
def test_folded_security_header_values_are_rejected(field: str) -> None:
    values = {
        "timestamp": str(TIMESTAMP).encode("ascii"),
        "event_id": EVENT_ID.encode("ascii"),
        "signature": SIGNATURE.encode("ascii"),
    }
    values[field] += b"\r\n continuation"

    assert (
        parse_webhook_auth_headers(
            _auth_headers(
                timestamp=values["timestamp"],
                event_id=values["event_id"],
                signature=values["signature"],
            )
        )
        is None
    )


@pytest.mark.parametrize(
    ("timestamp", "accepted"),
    [
        (TIMESTAMP - 301, False),
        (TIMESTAMP - 300, True),
        (TIMESTAMP, True),
        (TIMESTAMP + 300, True),
        (TIMESTAMP + 301, False),
    ],
)
def test_timestamp_window_is_inclusive_at_exactly_five_minutes(
    timestamp: int,
    accepted: bool,
) -> None:
    now = datetime.fromtimestamp(TIMESTAMP, tz=UTC)

    assert is_webhook_timestamp_current(timestamp=timestamp, now=now) is accepted


def test_fractional_server_clock_does_not_extend_the_past_timestamp_window() -> None:
    now = datetime.fromtimestamp(TIMESTAMP + 0.9, tz=UTC)

    assert not is_webhook_timestamp_current(timestamp=TIMESTAMP - 300, now=now)
    assert is_webhook_timestamp_current(timestamp=TIMESTAMP - 299, now=now)


def test_timestamp_window_requires_an_aware_server_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_webhook_timestamp_current(
            timestamp=TIMESTAMP,
            now=datetime(2026, 10, 7, 12, 0),
        )


@pytest.mark.parametrize(
    ("master_secret", "integration_id", "key_version"),
    [
        (b"x" * 31, INTEGRATION_ID, KEY_VERSION),
        (MASTER_SECRET, INTEGRATION_ID.upper(), KEY_VERSION),
        (MASTER_SECRET, INTEGRATION_ID.replace("-", ""), KEY_VERSION),
        (MASTER_SECRET, INTEGRATION_ID, 0),
        (MASTER_SECRET, INTEGRATION_ID, -1),
        (MASTER_SECRET, INTEGRATION_ID, True),
    ],
)
def test_key_derivation_rejects_invalid_internal_metadata(
    master_secret: bytes,
    integration_id: str,
    key_version: int,
) -> None:
    with pytest.raises(ValueError):
        derive_webhook_integration_key(
            master_secret,
            integration_id=integration_id,
            key_version=key_version,
        )


@pytest.mark.parametrize("key_version", [1.0, 1.5, "1"])
def test_key_derivation_rejects_non_integer_key_versions(key_version: object) -> None:
    with pytest.raises(ValueError):
        derive_webhook_integration_key(
            MASTER_SECRET,
            integration_id=INTEGRATION_ID,
            key_version=cast(int, key_version),
        )


@pytest.mark.parametrize(
    ("timestamp", "integration_id", "event_id"),
    [
        (cast(int, True), INTEGRATION_ID, EVENT_ID),
        (0, INTEGRATION_ID, EVENT_ID),
        (10_000_000_000, INTEGRATION_ID, EVENT_ID),
        (TIMESTAMP, INTEGRATION_ID.upper(), EVENT_ID),
        (TIMESTAMP, INTEGRATION_ID, "evt_1234567"),
        (TIMESTAMP, INTEGRATION_ID, "evt_A1b2C3d4\nforged"),
    ],
)
def test_signer_rejects_noncanonical_authentication_components(
    timestamp: int,
    integration_id: str,
    event_id: str,
) -> None:
    with pytest.raises(ValueError):
        build_webhook_signed_bytes(
            timestamp=timestamp,
            integration_id=integration_id,
            event_id=event_id,
            raw_body=RAW_BODY,
        )


def test_signer_validation_error_does_not_retain_non_ascii_event_id() -> None:
    event_id = "evt_SECRET\u00e9MARKER"

    with pytest.raises(ValueError) as raised:
        build_webhook_signed_bytes(
            timestamp=TIMESTAMP,
            integration_id=INTEGRATION_ID,
            event_id=event_id,
            raw_body=RAW_BODY,
        )

    assert str(raised.value) == "webhook event id must be canonical ASCII"
    assert event_id not in repr(raised.value)
    assert raised.value.__context__ is None
