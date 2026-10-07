"""Explicit exception-case state transitions."""

from typing import Literal

CaseStatus = Literal["open", "assigned", "resolved"]


class InvalidCaseTransition(Exception):
    """The requested command cannot run from the current terminal state."""


def _require_active(status: str) -> None:
    if status not in {"open", "assigned"}:
        raise InvalidCaseTransition


def state_after_assignment(status: str) -> CaseStatus:
    _require_active(status)
    return "assigned"


def state_after_resolution(status: str) -> CaseStatus:
    _require_active(status)
    return "resolved"


def validate_note_state(status: str) -> None:
    _require_active(status)
