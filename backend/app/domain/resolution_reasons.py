"""Resolution reasons intentionally form a small server-owned vocabulary."""

from typing import Final

RESOLUTION_REASONS: Final[dict[str, tuple[str, ...]]] = {
    "payment_failed": (
        "payment_recovered",
        "customer_contacted",
        "order_cancelled",
    ),
    "refund_review": (
        "refund_approved",
        "refund_rejected",
        "more_information_requested",
    ),
    "fulfillment_delayed": (
        "carrier_updated",
        "replacement_arranged",
        "customer_contacted",
    ),
}


def resolution_reason_is_allowed(rule_key: str, reason: str) -> bool:
    return reason in RESOLUTION_REASONS.get(rule_key, ())
