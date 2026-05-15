"""T-EM.1 — embedding-model lifecycle state machine (B50).

State machine drives the `promote → cutover → commit | rollback → abort` flow
documented in `docs/team/2026_05_15_embedding_model_lifecycle.md` §3.

Pure logic: input `(state, action)` → output next state, or raise
`IllegalEmbeddingTransition`. No I/O, no enums tied to DB schema (state itself
is *derived* from `system_settings` rows in the persistence layer).
"""

import pytest

VALID_TRANSITIONS = [
    ("IDLE", "promote", "CANDIDATE"),
    ("CANDIDATE", "cutover", "CUTOVER"),
    ("CANDIDATE", "abort", "IDLE"),
    ("CUTOVER", "rollback", "CANDIDATE"),
    ("CUTOVER", "commit", "IDLE"),
]

INVALID_TRANSITIONS = [
    # IDLE accepts only `promote`
    ("IDLE", "cutover"),
    ("IDLE", "rollback"),
    ("IDLE", "commit"),
    ("IDLE", "abort"),
    # CANDIDATE forbids `promote` (one-candidate-at-a-time invariant),
    # `rollback` (haven't cutover yet), and `commit` (haven't cutover yet).
    ("CANDIDATE", "promote"),
    ("CANDIDATE", "rollback"),
    ("CANDIDATE", "commit"),
    # CUTOVER forbids `promote`, `cutover`, `abort` (abort requires
    # rollback first so no reader is left on the to-be-retired field).
    ("CUTOVER", "promote"),
    ("CUTOVER", "cutover"),
    ("CUTOVER", "abort"),
]


@pytest.mark.parametrize("state,action,expected_next", VALID_TRANSITIONS)
def test_valid_transition_returns_next_state(state: str, action: str, expected_next: str) -> None:
    from ragent.utility.embedding_lifecycle import next_state

    assert next_state(state, action) == expected_next


@pytest.mark.parametrize("state,action", INVALID_TRANSITIONS)
def test_invalid_transition_raises(state: str, action: str) -> None:
    from ragent.utility.embedding_lifecycle import (
        IllegalEmbeddingTransition,
        next_state,
    )

    with pytest.raises(IllegalEmbeddingTransition):
        next_state(state, action)


def test_illegal_transition_message_includes_state_and_action() -> None:
    from ragent.utility.embedding_lifecycle import (
        IllegalEmbeddingTransition,
        next_state,
    )

    with pytest.raises(IllegalEmbeddingTransition) as exc_info:
        next_state("CUTOVER", "abort")
    msg = str(exc_info.value)
    assert "CUTOVER" in msg
    assert "abort" in msg


def test_unknown_state_rejected() -> None:
    from ragent.utility.embedding_lifecycle import (
        IllegalEmbeddingTransition,
        next_state,
    )

    with pytest.raises(IllegalEmbeddingTransition):
        next_state("BOGUS", "promote")


def test_unknown_action_rejected() -> None:
    from ragent.utility.embedding_lifecycle import (
        IllegalEmbeddingTransition,
        next_state,
    )

    with pytest.raises(IllegalEmbeddingTransition):
        next_state("IDLE", "explode")
