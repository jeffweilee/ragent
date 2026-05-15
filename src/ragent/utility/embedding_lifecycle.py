"""Embedding-model lifecycle state machine (B50).

Pure logic over the `IDLE ⇄ CANDIDATE ⇄ CUTOVER` flow that backs the five
admin endpoints in `routers/admin_embedding.py`. The live state is *derived*
from `system_settings` rows by the persistence layer; this module only
validates transitions and reports the next state.

Spec: `docs/team/2026_05_15_embedding_model_lifecycle.md` §3.
"""

_TRANSITIONS: dict[tuple[str, str], str] = {
    ("IDLE", "promote"): "CANDIDATE",
    ("CANDIDATE", "cutover"): "CUTOVER",
    ("CANDIDATE", "abort"): "IDLE",
    ("CUTOVER", "rollback"): "CANDIDATE",
    ("CUTOVER", "commit"): "IDLE",
}


class IllegalEmbeddingTransition(Exception):
    """Raised when `(state, action)` is not a legal lifecycle move."""


def next_state(state: str, action: str) -> str:
    """Return the state reached by `action` from `state`.

    Raises `IllegalEmbeddingTransition` for any unknown state, unknown action,
    or disallowed pair.
    """
    try:
        return _TRANSITIONS[(state, action)]
    except KeyError as exc:
        raise IllegalEmbeddingTransition(
            f"{state} -[{action}]-> is not a valid embedding-lifecycle transition"
        ) from exc
