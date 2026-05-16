"""T-FB.3 / T-FB.4 — FeedbackRepository: append-only `feedback` events (B50/B51).

MariaDB is the source of truth for user feedback. The ES `feedback_v1`
serving view (T-FB.5) is derived from these rows and recoverable from them
on rare write failures.

Per `docs/00_rule.md` Database Practices: every method checks out a fresh
async connection from the engine's pool and releases it on exit.
"""

from __future__ import annotations

from typing import Any


class FeedbackRepository:
    """Skeleton wired in T-FB.3; ``upsert`` lands in T-FB.4 (Red+Green)."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
