"""Closed media and JSON contracts for the synthetic webhook payload."""

from __future__ import annotations

import json
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from app.domain.webhook_event import (
    InvalidWebhookPayload,
    SyntheticWebhookEventPayload,
    UnsupportedWebhookMediaType,
    parse_synthetic_webhook_event,
)

VALID_BODY = (
    b'{"type":"payment.failed","occurred_at":"2026-10-07T12:34:56Z",'
    b'"data":{"order":{"id":"syn_order_A1B2C3D4","number":"DEMO-1045",'
    b'"amount_minor":12900,"currency":"USD"}}}'
)
JSON_HEADERS = [(b"content-type", b"application/json")]


def _valid_document() -> dict[str, object]:
    return {
        "type": "payment.failed",
        "occurred_at": "2026-10-07T12:34:56Z",
        "data": {
            "order": {
                "id": "syn_order_A1B2C3D4",
                "number": "DEMO-1045",
                "amount_minor": 12_900,
                "currency": "USD",
            }
        },
    }


def _order(document: dict[str, object]) -> dict[str, object]:
    data = cast(dict[str, object], document["data"])
    return cast(dict[str, object], data["order"])


def _encode(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse(
    raw_body: bytes = VALID_BODY,
    raw_headers: Sequence[tuple[bytes, bytes]] = JSON_HEADERS,
) -> SyntheticWebhookEventPayload:
    return parse_synthetic_webhook_event(raw_body=raw_body, raw_headers=raw_headers)


def _assert_invalid_payload(raw_body: bytes) -> None:
    with pytest.raises(InvalidWebhookPayload) as raised:
        _parse(raw_body)

    assert str(raised.value) == "invalid webhook payload"


def test_valid_payload_is_typed_closed_and_normalized_to_utc() -> None:
    payload = _parse()

    assert payload.type == "payment.failed"
    assert payload.occurred_at == datetime(2026, 10, 7, 12, 34, 56, tzinfo=UTC)
    assert payload.data.order.id == "syn_order_A1B2C3D4"
    assert payload.data.order.number == "DEMO-1045"
    assert payload.data.order.amount_minor == 12_900
    assert payload.data.order.currency == "USD"
    for model in (payload, payload.data, payload.data.order):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
        assert model.model_config["hide_input_in_errors"] is True


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/json",
        b"Application/JSON",
        b"application/json;charset=utf-8",
        b"APPLICATION/JSON ; CHARSET=UTF-8",
        b"\tapplication/json; charset=utf-8 ",
    ],
)
def test_closed_json_media_type_accepts_only_its_canonical_variants(
    content_type: bytes,
) -> None:
    payload = _parse(raw_headers=[(b"Content-Type", content_type), (b"x-extra", b"ignored")])

    assert payload.type == "payment.failed"


@pytest.mark.parametrize(
    "raw_headers",
    [
        [],
        [(b"content-type", b"")],
        [
            (b"content-type", b"application/json"),
            (b"Content-Type", b"application/json"),
        ],
        [(b"content-type", b"text/json")],
        [(b"content-type", b"application/problem+json")],
        [(b"content-type", b"application/json, application/json")],
        [(b"content-type", b"application/json; charset=utf8")],
        [(b"content-type", b"application/json; charset=utf-16")],
        [(b"content-type", b'application/json; charset="utf-8"')],
        [(b"content-type", b"application/json; charset =utf-8")],
        [(b"content-type", b"application/json; charset= utf-8")],
        [(b"content-type", b"application/json; charset=utf-8; version=1")],
        [(b"content-type", b"application/json; charset=utf-8; charset=utf-8")],
        [(b"content-type", b"application/json\r\n charset=utf-8")],
        [(b"content-type", b"application/json\xff")],
    ],
)
def test_unsupported_or_ambiguous_content_types_are_rejected(
    raw_headers: list[tuple[bytes, bytes]],
) -> None:
    with pytest.raises(UnsupportedWebhookMediaType) as raised:
        _parse(raw_headers=raw_headers)

    assert str(raised.value) == "unsupported webhook media type"


@pytest.mark.parametrize("encoding", [b"", b"identity", b"gzip"])
def test_content_encoding_must_be_absent(encoding: bytes) -> None:
    with pytest.raises(UnsupportedWebhookMediaType):
        _parse(
            raw_headers=[
                (b"content-type", b"application/json"),
                (b"Content-Encoding", encoding),
            ]
        )


def test_media_contract_is_checked_before_body_decoding() -> None:
    with pytest.raises(UnsupportedWebhookMediaType):
        _parse(raw_body=b"\xff", raw_headers=[(b"content-type", b"text/plain")])


def test_json_allows_surrounding_insignificant_whitespace() -> None:
    payload = _parse(b" \t\r\n" + VALID_BODY + b"\n ")

    assert payload.data.order.id == "syn_order_A1B2C3D4"


@pytest.mark.parametrize(
    "raw_body",
    [
        b"\xff",
        VALID_BODY[:-1] + b"\xc3",
        b"\xef\xbb\xbf" + VALID_BODY,
        b"",
        b" \t\r\n",
        VALID_BODY.replace(
            b'{"type":"payment.failed"',
            b'{"type":"payment.failed","type":"payment.failed"',
            1,
        ),
        VALID_BODY.replace(
            b'"currency":"USD"',
            b'"currency":"USD","currency":"USD"',
            1,
        ),
        VALID_BODY.replace(
            b'{"type":"payment.failed"',
            b'{"type":"payment.failed","\\u0074ype":"payment.failed"',
            1,
        ),
        VALID_BODY.replace(b"12900", b"NaN", 1),
        VALID_BODY.replace(b"12900", b"Infinity", 1),
        VALID_BODY.replace(b"12900", b"-Infinity", 1),
        VALID_BODY + b"{}",
        VALID_BODY + b" trailing",
        VALID_BODY[:-1] + b",}",
        b"/* comment */" + VALID_BODY,
    ],
)
def test_malformed_unicode_or_nonstandard_json_is_rejected(raw_body: bytes) -> None:
    _assert_invalid_payload(raw_body)


@pytest.mark.parametrize("raw_body", [b"[]", b'"value"', b"1", b"true", b"null"])
def test_json_root_must_be_an_object(raw_body: bytes) -> None:
    _assert_invalid_payload(raw_body)


def test_excessively_nested_json_is_rejected_without_a_recursion_error() -> None:
    depth = 2_000
    raw_body = b'{"type":' + b"[" * depth + b"0" + b"]" * depth + b"}"

    _assert_invalid_payload(raw_body)


@pytest.mark.parametrize("layer", ["root", "data", "order"])
def test_every_payload_layer_rejects_unexpected_fields_without_reflection(layer: str) -> None:
    marker = f"forbidden-{layer}-SECRET-MARKER"
    document = _valid_document()
    data = cast(dict[str, object], document["data"])
    layers = {"root": document, "data": data, "order": _order(document)}
    layers[layer]["unexpected"] = marker
    body = _encode(document)

    with pytest.raises(InvalidWebhookPayload) as raised:
        _parse(body)

    rendered = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert str(raised.value) == "invalid webhook payload"
    assert marker not in repr(raised.value)
    assert marker not in rendered


@pytest.mark.parametrize(
    "path",
    [
        ("type",),
        ("occurred_at",),
        ("data",),
        ("data", "order"),
        ("data", "order", "id"),
        ("data", "order", "number"),
        ("data", "order", "amount_minor"),
        ("data", "order", "currency"),
    ],
)
def test_every_payload_field_is_required(path: tuple[str, ...]) -> None:
    document = _valid_document()
    container = document
    for component in path[:-1]:
        container = cast(dict[str, object], container[component])
    del container[path[-1]]

    _assert_invalid_payload(_encode(document))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("data",), None),
        (("data",), []),
        (("data", "order"), None),
        (("data", "order"), []),
    ],
)
def test_nested_payload_layers_must_be_objects(
    path: tuple[str, ...],
    value: object,
) -> None:
    document = _valid_document()
    container = document
    for component in path[:-1]:
        container = cast(dict[str, object], container[component])
    container[path[-1]] = value

    _assert_invalid_payload(_encode(document))


@pytest.mark.parametrize("event_type", ["Payment.Failed", "payment_failed", "refund.failed", 1])
def test_only_payment_failed_event_type_is_accepted(event_type: object) -> None:
    document = _valid_document()
    document["type"] = event_type

    _assert_invalid_payload(_encode(document))


@pytest.mark.parametrize("suffix_length", [8, 48])
def test_synthetic_order_id_accepts_both_length_boundaries(suffix_length: int) -> None:
    document = _valid_document()
    order_id = "syn_order_" + "A" * suffix_length
    _order(document)["id"] = order_id

    assert _parse(_encode(document)).data.order.id == order_id


@pytest.mark.parametrize(
    "order_id",
    [
        "syn_order_" + "A" * 7,
        "syn_order_" + "A" * 49,
        "syn_order_A1B2_C3D4",
        "syn_order_A1B2-C3D4",
        "syn_order_\uff21\u0031B2C3D4",
        "syn_order_A1B2C3D4 ",
        "order_A1B2C3D4",
        "ord_live12345678",
        "gid://provider/Order/12345678",
    ],
)
def test_live_looking_or_noncanonical_order_ids_are_rejected(order_id: str) -> None:
    document = _valid_document()
    _order(document)["id"] = order_id

    _assert_invalid_payload(_encode(document))


@pytest.mark.parametrize("digits", [4, 10])
def test_demo_order_number_accepts_both_length_boundaries(digits: int) -> None:
    document = _valid_document()
    number = "DEMO-" + "1" * digits
    _order(document)["number"] = number

    assert _parse(_encode(document)).data.order.number == number


@pytest.mark.parametrize(
    "number",
    [
        "DEMO-123",
        "DEMO-12345678901",
        "DEMO-\uff11\uff12\uff13\uff14",
        "demo-1234",
        "LIVE-1234",
        "DEMO-1234 ",
    ],
)
def test_noncanonical_or_live_order_numbers_are_rejected(number: str) -> None:
    document = _valid_document()
    _order(document)["number"] = number

    _assert_invalid_payload(_encode(document))


@pytest.mark.parametrize("amount", [0, 999_999_999])
def test_amount_minor_accepts_both_integer_boundaries(amount: int) -> None:
    document = _valid_document()
    _order(document)["amount_minor"] = amount

    assert _parse(_encode(document)).data.order.amount_minor == amount


@pytest.mark.parametrize(
    "raw_amount", [b"-1", b"1000000000", b"true", b"false", b"0.0", b"0e0", b'"0"', b"null"]
)
def test_amount_minor_rejects_out_of_range_or_non_strict_integers(raw_amount: bytes) -> None:
    body = VALID_BODY.replace(b"12900", raw_amount, 1)

    _assert_invalid_payload(body)


def test_negative_zero_is_a_valid_json_integer_with_zero_value() -> None:
    payload = _parse(VALID_BODY.replace(b"12900", b"-0", 1))

    assert payload.data.order.amount_minor == 0


@pytest.mark.parametrize("currency", ["AAA", "USD", "ZZZ"])
def test_currency_accepts_exactly_three_uppercase_ascii_letters(currency: str) -> None:
    document = _valid_document()
    _order(document)["currency"] = currency

    assert _parse(_encode(document)).data.order.currency == currency


@pytest.mark.parametrize("currency", ["US", "USDD", "usd", "US1", "ÜSD", " USD", "USD\n"])
def test_noncanonical_currencies_are_rejected(currency: str) -> None:
    document = _valid_document()
    _order(document)["currency"] = currency

    _assert_invalid_payload(_encode(document))


@pytest.mark.parametrize(
    ("occurred_at", "expected"),
    [
        (
            "2026-10-07T12:34:56Z",
            datetime(2026, 10, 7, 12, 34, 56, tzinfo=UTC),
        ),
        (
            "2026-10-07T14:34:56+02:00",
            datetime(2026, 10, 7, 12, 34, 56, tzinfo=UTC),
        ),
        (
            "2026-10-07T07:04:56.123456-05:30",
            datetime(2026, 10, 7, 12, 34, 56, 123456, tzinfo=UTC),
        ),
    ],
)
def test_occurred_at_requires_an_offset_and_normalizes_to_utc(
    occurred_at: str,
    expected: datetime,
) -> None:
    document = _valid_document()
    document["occurred_at"] = occurred_at

    assert _parse(_encode(document)).occurred_at == expected


@pytest.mark.parametrize(
    "occurred_at",
    [
        "2026-10-07T12:34:56",
        "2026-10-07",
        1_791_376_496,
        "1791376496",
        True,
        None,
        "2026-13-07T12:34:56Z",
        "2026-10-07T12:34:60Z",
        "2026-10-07T12:34:56+25:00",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ],
)
def test_naive_non_string_or_invalid_occurrence_times_are_rejected(
    occurred_at: object,
) -> None:
    document = _valid_document()
    document["occurred_at"] = occurred_at

    _assert_invalid_payload(_encode(document))


def test_invalid_field_values_are_not_reflected_by_the_public_exception() -> None:
    marker = "live_order_SECRET_VALUE_987654321"
    document = _valid_document()
    _order(document)["id"] = marker

    with pytest.raises(InvalidWebhookPayload) as raised:
        _parse(_encode(document))

    rendered = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert str(raised.value) == "invalid webhook payload"
    assert marker not in repr(raised.value)
    assert marker not in rendered
    assert raised.value.__context__ is None


@pytest.mark.parametrize("raw_body", [b"\xff", b'{"unterminated":'])
def test_low_level_parser_exceptions_are_not_retained(raw_body: bytes) -> None:
    with pytest.raises(InvalidWebhookPayload) as raised:
        _parse(raw_body)

    assert raised.value.__context__ is None


def test_unsupported_media_value_is_not_reflected_by_the_public_exception() -> None:
    marker = b"application/SECRET-MEDIA-MARKER"

    with pytest.raises(UnsupportedWebhookMediaType) as raised:
        _parse(raw_headers=[(b"content-type", marker)])

    assert str(raised.value) == "unsupported webhook media type"
    assert marker.decode("ascii") not in repr(raised.value)
