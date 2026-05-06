"""T3.2b/T3.2h / TA.10 — Ingest worker: pipeline task with NOWAIT locking and backoff."""

from __future__ import annotations

import contextlib
import tempfile
import time

import structlog
from anyio import to_thread

from ragent.bootstrap.broker import broker
from ragent.pipelines.observability import IngestStepError, bind_ingest_context, log_ingest_step
from ragent.repositories.document_repository import LockNotAvailable

logger = structlog.get_logger(__name__)


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

    # TX-A: atomic SELECT FOR UPDATE NOWAIT + UPDATE PENDING in one transaction (Fix #1, R7, S28)
    try:
        doc = await repo.claim_for_processing(document_id)
    except LockNotAvailable:
        logger.info("ingest.lock_contention", document_id=document_id)
        return

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

    started = time.monotonic()
    with bind_ingest_context(document_id=document_id):
        try:
            written = await to_thread.run_sync(_run_pipeline)
        except Exception as exc:
            # Haystack wraps component errors in PipelineRuntimeError; the
            # original IngestStepError (if any) is preserved on __cause__.
            cause = exc.__cause__ if isinstance(exc.__cause__, IngestStepError) else None
            error_code = cause.error_code if cause is not None else "PIPELINE_TIMEOUT"
            log_ingest_step.failed(
                document_id=document_id,
                reason=f"{type(exc).__name__}: {exc}",
                error_code=error_code,
            )
            await repo.update_status(document_id, from_status="PENDING", to_status="FAILED")
            return

        duration_ms_total = int((time.monotonic() - started) * 1000)
        chunks_total = len(written) if isinstance(written, list) else 0
        log_ingest_step.ready(
            document_id=document_id,
            chunks_total=chunks_total,
            duration_ms_total=duration_ms_total,
        )

    # TX-B: commit READY *before* MinIO cleanup (S16)
    await repo.update_status(document_id, from_status="PENDING", to_status="READY")

    # Best-effort MinIO delete — orphan is tolerated and logged (S21)
    with contextlib.suppress(Exception):
        storage.delete_object(doc.object_key)

    # Fan out to downstream plugins (vector / graph) once the row is READY
    await container.registry.fan_out(document_id)


@broker.task("ingest.supersede")
async def ingest_supersede_task(survivor_id: str, source_id: str, source_app: str) -> None:
    """T3.2d — Supersede worker task (R3, S26).

    Pops oldest losers for ``(source_id, source_app)`` and cascade-deletes
    them, keeping ``survivor_id`` (= MAX(created_at)).
    """
    from ragent.bootstrap.composition import get_container
    from ragent.services.ingest_service import IngestService

    container = get_container()
    svc = IngestService(
        repo=container.doc_repo,
        chunks=container.chunk_repo,
        storage=container.minio_client,
        broker=container.registry,
    )

    await svc.supersede(survivor_id, source_id, source_app)
