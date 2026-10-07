"""The public demo exposes a fixed, deterministic exception rule catalog."""

from datetime import timedelta

from app.domain.case_rules import CASE_RULES


def test_case_rules_match_the_published_demo_contract() -> None:
    assert {
        key: (
            rule.case_type,
            rule.severity,
            rule.sla,
            rule.resolution_reasons,
        )
        for key, rule in CASE_RULES.items()
    } == {
        "payment_failed": (
            "payment",
            "high",
            timedelta(hours=2),
            (
                "payment_recovered",
                "customer_contacted",
                "order_cancelled",
            ),
        ),
        "refund_review": (
            "refund",
            "medium",
            timedelta(hours=4),
            (
                "refund_approved",
                "refund_rejected",
                "more_information_requested",
            ),
        ),
        "fulfillment_delayed": (
            "fulfillment",
            "high",
            timedelta(hours=2),
            (
                "carrier_updated",
                "replacement_arranged",
                "customer_contacted",
            ),
        ),
    }
