"""Case transitions are intentionally smaller than a generic workflow engine."""

import pytest

from app.domain.case_state import (
    InvalidCaseTransition,
    state_after_assignment,
    state_after_resolution,
    validate_note_state,
)


def test_only_open_and_assigned_cases_accept_workflow_commands() -> None:
    assert state_after_assignment("open") == "assigned"
    assert state_after_assignment("assigned") == "assigned"
    assert state_after_resolution("open") == "resolved"
    assert state_after_resolution("assigned") == "resolved"
    validate_note_state("open")
    validate_note_state("assigned")

    for operation in (
        state_after_assignment,
        state_after_resolution,
        validate_note_state,
    ):
        with pytest.raises(InvalidCaseTransition):
            operation("resolved")
