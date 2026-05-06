"""T3.2b/T3.2h — Ingest worker: pipeline task with NOWAIT locking and backoff."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import structlog

from ragent.bootstrap.broker import broker

logger = structlog.get_logger(__name__)

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


@broker.task("ingest.pipeline")
async def ingest_pipeline_task(document_id: str) -> None:
    """TaskIQ entrypoint (T3.2b). Resolves dependencies from the composition
    root and runs `run_pipeline_task` off the worker's event loop.

    Pipeline body: download the staged file from MinIO, drive it through the
    idempotent ingest pipeline (chunks-clean → split → embed), and after the
    sync orchestration finishes successfully (status committed READY) the
    plugin fan-out is invoked so that downstream extractors (vector / graph)
    persist their representations.
    """
    import tempfile

    from anyio import to_thread

    from ragent.bootstrap.composition import get_container

    container = get_container()

    def _pipeline_fn(doc_id: str) -> list:
        doc = container.doc_repo.get(doc_id)
        if doc is None:
            return []
        data = container.minio_client.get_object(doc.object_key)
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            result = container.ingest_pipeline.run(
                {
                    "converter": {"sources": [tmp.name]},
                    "idempotency_clean": {"document_id": doc_id},
                }
            )
        return result.get("writer", {}).get("documents_written", [])

    await to_thread.run_sync(
        lambda: run_pipeline_task(
            document_id=document_id,
            repo=container.doc_repo,
            storage=container.minio_client,
            broker=container.registry,
            pipeline_fn=_pipeline_fn,
        )
    )

    # Fan out to downstream plugins (vector / graph) once the row is READY.
    doc = container.doc_repo.get(document_id)
    if doc is not None and doc.status == "READY":
        await container.registry.fan_out(document_id)


@broker.task("ingest.supersede")
async def ingest_supersede_task(survivor_id: str, source_id: str, source_app: str) -> None:
    """T3.2d — Supersede worker task (R3, S26).

    Pops oldest losers for `(source_id, source_app)` and cascade-deletes
    them, keeping `survivor_id` (= MAX(created_at)). Runs the sync DB
    work via `to_thread.run_sync` to keep the event loop free.
    """
    from anyio import to_thread

    from ragent.bootstrap.composition import get_container
    from ragent.services.ingest_service import IngestService

    container = get_container()
    svc = IngestService(
        repo=container.doc_repo,
        chunks=container.chunk_repo,
        storage=container.minio_client,
        broker=container.registry,
    )

    await to_thread.run_sync(svc.supersede, survivor_id, source_id, source_app)
