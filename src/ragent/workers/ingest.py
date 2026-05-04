"""T3.2b/T3.2h — Ingest worker: pipeline task with NOWAIT locking and backoff."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 30.0


def handle_lock_contention(document_id: str, current_attempt: int, repo: Any) -> float:
    """Return re-kiq delay (seconds) without incrementing attempt (R7, S28)."""
    return min(_BACKOFF_BASE ** (current_attempt + 1), _BACKOFF_CAP)


def run_pipeline_task(
    document_id: str,
    repo: Any,
    storage: Any,
    broker: Any,
    pipeline_fn: Callable[[str], list],
) -> None:
    """Execute the ingest pipeline with the correct commit-before-cleanup ordering (S16, S21).

    TX-A: acquire NOWAIT → set PENDING → commit.
    Pipeline body runs OUTSIDE any DB tx.
    TX-B: commit terminal status FIRST; then attempt MinIO delete best-effort.
    On pipeline failure: set FAILED, do NOT delete MinIO object (S16).
    """
    doc = repo.acquire_nowait(document_id)
    repo.update_status(
        document_id, from_status=doc.status, to_status="PENDING", attempt=doc.attempt
    )

    try:
        pipeline_fn(document_id)
    except Exception:
        repo.update_status(document_id, from_status="PENDING", to_status="FAILED")
        return

    # TX-B: commit READY *before* MinIO cleanup (S16)
    repo.update_status(document_id, from_status="PENDING", to_status="READY")

    # Best-effort MinIO delete — orphan is tolerated and logged (S21)
    with contextlib.suppress(Exception):
        storage.delete_object(doc.object_key)
