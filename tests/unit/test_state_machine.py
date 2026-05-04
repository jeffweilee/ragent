"""T0.7 — state machine validates document status transitions (spec S10)."""

import pytest

VALID_TRANSITIONS = [
    ("UPLOADED", "PENDING"),
    ("PENDING", "READY"),
    ("PENDING", "FAILED"),
    ("PENDING", "DELETING"),
    ("READY", "DELETING"),
    ("FAILED", "DELETING"),
]

INVALID_TRANSITIONS = [
    ("UPLOADED", "FAILED"),
    ("READY", "PENDING"),
    ("FAILED", "READY"),
    ("DELETING", "READY"),
]


@pytest.mark.parametrize("from_status,to_status", VALID_TRANSITIONS)
def test_valid_transition_accepted(from_status: str, to_status: str) -> None:
    from ragent.utility.state_machine import assert_transition

    assert_transition(from_status, to_status)  # must not raise


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITIONS)
def test_invalid_transition_raises(from_status: str, to_status: str) -> None:
    from ragent.utility.state_machine import IllegalStateTransition, assert_transition

    with pytest.raises(IllegalStateTransition):
        assert_transition(from_status, to_status)


def test_illegal_state_transition_carries_states() -> None:
    from ragent.utility.state_machine import IllegalStateTransition, assert_transition

    with pytest.raises(IllegalStateTransition) as exc_info:
        assert_transition("READY", "PENDING")
    assert "READY" in str(exc_info.value)
    assert "PENDING" in str(exc_info.value)
