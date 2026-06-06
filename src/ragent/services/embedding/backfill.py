"""Standalone backfill trigger for the embedding lifecycle (spec §5 B50)."""

from __future__ import annotations

from typing import Any

import structlog

from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

logger = structlog.get_logger(__name__)


async def backfill(registry: Any, *, broker: Any) -> dict:
    logger.info("embedding.lifecycle.backfill.started")
    try:
        state = registry.derived_state()
        if state not in ("CANDIDATE", "CUTOVER"):
            raise IllegalEmbeddingTransition(
                f"backfill requires CANDIDATE or CUTOVER state, got {state}"
            )
        candidate_index = registry.candidate_index
        if not candidate_index:
            raise IllegalEmbeddingTransition("backfill: candidate has no index_name")
        stable_index = registry.stable_index
        await broker.enqueue(
            "ingest.backfill_candidate",
            stable_index=stable_index,
            candidate_index=candidate_index,
        )
    except Exception as exc:
        logger.warning(
            "embedding.lifecycle.backfill.failed",
            error_code=type(exc).__name__,
            error_reason=str(exc)[:128],
        )
        raise
    logger.info("embedding.lifecycle.backfill.completed", candidate_index=candidate_index)
    return {
        "state": state,
        "queued": True,
        "stable_index": stable_index,
        "candidate_index": candidate_index,
    }
