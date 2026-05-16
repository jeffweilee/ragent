"""T-FB.3 / T-FB.4 — FeedbackRepository: append-only `feedback` events (B50/B51).

MariaDB is the source of truth for user feedback. The ES `feedback_v1`
serving view (T-FB.5) is derived from these rows and recoverable from them
on rare write failures.

Per `docs/00_rule.md` Database Practices: every method checks out a fresh
async connection from the engine's pool and releases it on exit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ragent.utility.datetime import utcnow
from ragent.utility.id_gen import new_id

_UPSERT_SQL = text(
    """
    INSERT INTO feedback (
      feedback_id, request_id, user_id, source_id, vote, reason,
      position_shown, created_at, updated_at
    ) VALUES (
      :feedback_id, :request_id, :user_id, :source_id, :vote, :reason,
      :position_shown, :created_at, :updated_at
    )
    ON DUPLICATE KEY UPDATE
      vote = VALUES(vote),
      reason = VALUES(reason),
      position_shown = VALUES(position_shown),
      updated_at = VALUES(updated_at)
    """
)


class FeedbackRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def upsert(
        self,
        *,
        request_id: str,
        user_id: str,
        source_id: str,
        vote: int,
        reason: str | None,
        position_shown: int | None = None,
    ) -> str:
        """Insert a feedback row or overwrite the prior vote for the same triple.

        Idempotent on ``(user_id, request_id, source_id)`` (B51). Returns the
        new ``feedback_id``; on duplicate, the existing row is updated and
        the freshly minted id is returned but not persisted (caller treats
        it as opaque).
        """
        if vote not in (-1, 1):
            raise ValueError(f"vote must be ±1, got {vote!r}")
        now = utcnow()
        feedback_id = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                _UPSERT_SQL,
                {
                    "feedback_id": feedback_id,
                    "request_id": request_id,
                    "user_id": user_id,
                    "source_id": source_id,
                    "vote": vote,
                    "reason": reason,
                    "position_shown": position_shown,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return feedback_id
