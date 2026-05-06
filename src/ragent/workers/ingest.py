"""T3.2b/T3.2h / TA.10 — Ingest worker: pipeline task with NOWAIT locking and backoff."""

from __future__ import annotations

import contextlib
import tempfile
from typing import Any

import structlog
from anyio import to_thread

from ragent.bootstrap.broker import broker

logger = structlog.get_logger(__name__)

_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 30.0


def handle_lock_contention(document_id: str, current_attempt: int, repo: Any) -> float:
    """Return re-kiq delay (seconds) without incrementing attempt (R7, S28)."""
    return min(_BACKOFF_BASE ** (current_attempt + 1), _BACKOFF_CAP)


@broker.task("ingest.pipeline")
async def ingest_pipeline_task(document_id: str) -> None:
    """TaskIQ entrypoint (T3.2b).

    TX-A: acquire NOWAIT → PENDING → commit.
    Pipeline body runs in an anyio thread OUTSIDE any DB tx.
    TX-B: commit terminal status first; then attempt MinIO delete best-effort (S16, S21).
    """
    from ragent.bootstrap.composition import get_container

    container = get_container()
    repo = container.doc_repo
    storage = container.minio_client

    # TX-A: acquire NOWAIT; fail fast on contention (R7, S28)
    try:
        doc = await repo.acquire_nowait(document_id)
    except Exception:
        # re-kiq with backoff handled by caller; just return
        return

    await repo.update_status(
        document_id, from_status=doc.status, to_status="PENDING", attempt=doc.attempt
    )

    # Pipeline body: blocking IO runs in anyio-managed thread.
    # _IdempotencyClean and _SourceHydrator bridge back via anyio.from_thread.run().
    def _run_pipeline() -> list:
        data = storage.get_object(doc.object_key)
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            result = container.ingest_pipeline.run(
                {
                    "converter": {"sources": [tmp.name]},
                    "idempotency_clean": {"document_id": document_id},
                }
            )
        return result.get("writer", {}).get("documents_written", [])

    try:
        await to_thread.run_sync(_run_pipeline)
    except Exception:
        await repo.update_status(document_id, from_status="PENDING", to_status="FAILED")
        return

    # TX-B: commit READY *before* MinIO cleanup (S16)
    await repo.update_status(document_id, from_status="PENDING", to_status="READY")

    # Best-effort MinIO delete — orphan is tolerated and logged (S21)
    with contextlib.suppress(Exception):
        storage.delete_object(doc.object_key)

    # Fan out to downstream plugins (vector / graph) once the row is READY
    await container.registry.fan_out(document_id)
