"""Fixed case rules used by the synthetic demo and later event processing."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from app.domain.resolution_reasons import RESOLUTION_REASONS

CaseSeverity = Literal["high", "medium"]
CaseType = Literal["payment", "refund", "fulfillment"]


@dataclass(frozen=True)
class CaseRule:
    case_type: CaseType
    severity: CaseSeverity
    sla: timedelta
    resolution_reasons: tuple[str, ...]


CASE_RULES: Final[dict[str, CaseRule]] = {
    "payment_failed": CaseRule(
        case_type="payment",
        severity="high",
        sla=timedelta(hours=2),
        resolution_reasons=RESOLUTION_REASONS["payment_failed"],
    ),
    "refund_review": CaseRule(
        case_type="refund",
        severity="medium",
        sla=timedelta(hours=4),
        resolution_reasons=RESOLUTION_REASONS["refund_review"],
    ),
    "fulfillment_delayed": CaseRule(
        case_type="fulfillment",
        severity="high",
        sla=timedelta(hours=2),
        resolution_reasons=RESOLUTION_REASONS["fulfillment_delayed"],
    ),
}
