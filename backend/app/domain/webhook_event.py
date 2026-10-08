"""Strict media, JSON, and schema parsing for synthetic webhook events."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, NoReturn

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

CONTENT_TYPE_HEADER = b"content-type"
CONTENT_ENCODING_HEADER = b"content-encoding"
UTF8_BOM = b"\xef\xbb\xbf"
CONTENT_TYPE_PATTERN = re.compile(
    rb"application/json(?:[ \t]*;[ \t]*charset=utf-8)?",
    flags=re.ASCII | re.IGNORECASE,
)
ORDER_ID_PATTERN = re.compile(r"syn_order_[A-Za-z0-9]{8,48}", flags=re.ASCII)
ORDER_NUMBER_PATTERN = re.compile(r"DEMO-[0-9]{4,10}", flags=re.ASCII)
CURRENCY_PATTERN = re.compile(r"[A-Z]{3}", flags=re.ASCII)
INVALID_PAYLOAD_MESSAGE = "invalid webhook payload"
UNSUPPORTED_MEDIA_MESSAGE = "unsupported webhook media type"

StrictPayloadString = Annotated[str, StringConstraints(strict=True)]
StrictAmount = Annotated[int, Field(strict=True, ge=0, le=999_999_999)]


class UnsupportedWebhookMediaType(ValueError):
    """The request media metadata is outside the closed webhook contract."""


class InvalidWebhookPayload(ValueError):
    """The authenticated body is not a supported synthetic webhook event."""


class _ClosedPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class SyntheticOrderPayload(_ClosedPayload):
    id: StrictPayloadString
    number: StrictPayloadString
    amount_minor: StrictAmount
    currency: StrictPayloadString

    @field_validator("id")
    @classmethod
    def validate_synthetic_id(cls, value: str) -> str:
        if ORDER_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("order id must use the synthetic identifier grammar")
        return value

    @field_validator("number")
    @classmethod
    def validate_demo_number(cls, value: str) -> str:
        if ORDER_NUMBER_PATTERN.fullmatch(value) is None:
            raise ValueError("order number must use the demo identifier grammar")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if CURRENCY_PATTERN.fullmatch(value) is None:
            raise ValueError("currency must be three uppercase ASCII letters")
        return value


class SyntheticWebhookDataPayload(_ClosedPayload):
    order: SyntheticOrderPayload


class SyntheticWebhookEventPayload(_ClosedPayload):
    type: Literal["payment.failed"]
    occurred_at: datetime
    data: SyntheticWebhookDataPayload

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_aware_occurrence_time(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("occurred_at must be a timezone-aware string")
        try:
            occurred_at = datetime.fromisoformat(value)
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                raise ValueError
            return occurred_at.astimezone(UTC)
        except (ValueError, OverflowError):
            raise ValueError("occurred_at must be a timezone-aware string") from None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise InvalidWebhookPayload(INVALID_PAYLOAD_MESSAGE)
        document[key] = value
    return document


def _reject_nonstandard_constant(_value: str) -> NoReturn:
    raise InvalidWebhookPayload(INVALID_PAYLOAD_MESSAGE)


def _validate_media_type(raw_headers: Sequence[tuple[bytes, bytes]]) -> None:
    content_types: list[bytes] = []
    has_content_encoding = False
    for raw_name, raw_value in raw_headers:
        name = raw_name.lower()
        if name == CONTENT_TYPE_HEADER:
            content_types.append(raw_value)
        elif name == CONTENT_ENCODING_HEADER:
            has_content_encoding = True

    if has_content_encoding or len(content_types) != 1:
        raise UnsupportedWebhookMediaType(UNSUPPORTED_MEDIA_MESSAGE)
    content_type = content_types[0].strip(b" \t")
    if CONTENT_TYPE_PATTERN.fullmatch(content_type) is None:
        raise UnsupportedWebhookMediaType(UNSUPPORTED_MEDIA_MESSAGE)


def parse_synthetic_webhook_event(
    *,
    raw_body: bytes,
    raw_headers: Sequence[tuple[bytes, bytes]],
) -> SyntheticWebhookEventPayload:
    """Parse one authenticated event without retaining or reflecting request data."""

    _validate_media_type(raw_headers)
    try:
        if raw_body.startswith(UTF8_BOM):
            raise InvalidWebhookPayload(INVALID_PAYLOAD_MESSAGE)
        decoded_body = raw_body.decode("utf-8", errors="strict")
        document = json.loads(
            decoded_body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
        if not isinstance(document, dict):
            raise InvalidWebhookPayload(INVALID_PAYLOAD_MESSAGE)
        return SyntheticWebhookEventPayload.model_validate(document)
    except (
        InvalidWebhookPayload,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ):
        pass
    # Raising after the except suite prevents body-bearing parser errors from
    # being retained as the public exception's implicit context.
    raise InvalidWebhookPayload(INVALID_PAYLOAD_MESSAGE)
