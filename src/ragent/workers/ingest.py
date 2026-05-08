"""C4 / T2v.39 — V2 ingest worker.

TX-A: claim PENDING (NOWAIT). Pipeline body runs outside any DB tx —
worker fetches bytes from the right MinIO site, decodes UTF-8, feeds the
v2 pipeline (``loader → splitter → [idempotency_clean] → chunker →
embedder → writer``). TX-B: commit terminal status. For ``ingest_type ==
'inline'`` the staging object is best-effort deleted; ``ingest_type ==
'file'`` is caller-owned, never deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import structlog
from anyio import to_thread

from ragent.bootstrap.broker import broker
from ragent.bootstrap.metrics import observe_pipeline_duration, record_pipeline_outcome
from ragent.pipelines.observability import IngestStepError, bind_ingest_context, log_ingest_step
from ragent.repositories.document_repository import LockNotAvailable

logger = structlog.get_logger(__name__)

DEFAULT_MIME = "text/plain"


def _aggregate_timeout_seconds() -> float:
    return float(os.environ.get("INGEST_PIPELINE_TIMEOUT_SECONDS", "300"))


@broker.task("ingest.pipeline")
async def ingest_pipeline_task(document_id: str) -> None:
    from ragent.bootstrap.composition import get_container

    container = get_container()
    repo = container.doc_repo
    registry = container.minio_registry

    try:
        doc = await repo.claim_for_processing(document_id)
    except LockNotAvailable:
        logger.info("ingest.lock_contention", document_id=document_id)
        return

    site = doc.minio_site or "__default__"

    def _run_pipeline() -> int:
        # head_object recovers the content-type set at upload time. For file
        # ingests the caller's MinIO put metadata is the source of truth; for
        # inline ingests IngestService.create writes content_type explicitly.
        head = registry.head_object(site, doc.object_key)
        mime = (head[1] if head else None) or DEFAULT_MIME
        # Strip charset suffix etc. ("text/markdown; charset=utf-8" → "text/markdown").
        mime = mime.split(";", 1)[0].strip()

        # Pass HEAD size into get_object so a partial network read raises
        # rather than silently truncating the source document.
        expected_size = head[0] if head else None
        data = registry.get_object(site, doc.object_key, expected_size=expected_size)
        try:
            content = data.decode("utf-8")
            decode_replacements = 0
        except UnicodeDecodeError:
            # Fall back to lossy decode but report how many invalid sequences
            # were substituted (subtract genuine U+FFFD already in the source
            # so legitimate text containing the replacement char isn't false-
            # flagged as decode corruption).
            content = data.decode("utf-8", errors="replace")
            source_fffd = data.count(b"\xef\xbf\xbd")
            decode_replacements = max(0, content.count("\ufffd") - source_fffd)
            logger.warning(
                "ingest.utf8_decode_replaced",
                document_id=document_id,
                replacement_count=decode_replacements,
                size=len(data),
            )

        loader_kwargs = {
            "content": content,
            "mime_type": mime,
            "document_id": document_id,
            "source_url": doc.source_url,
            "source_title": doc.source_title,
            "source_app": doc.source_app,
            "source_meta": doc.source_meta,
        }
        result = container.ingest_pipeline.run({"loader": loader_kwargs})
        written = (result.get("writer") or {}).get("documents_written", 0)
        return written if isinstance(written, int) else len(written)

    started = time.monotonic()
    with bind_ingest_context(document_id=document_id):
        try:
            chunks_total = await asyncio.wait_for(
                to_thread.run_sync(_run_pipeline, abandon_on_cancel=True),
                timeout=_aggregate_timeout_seconds(),
            )
        except TimeoutError:
            log_ingest_step.failed(
                document_id=document_id,
                reason=f"aggregate pipeline timeout after {_aggregate_timeout_seconds()}s",
                error_code="PIPELINE_TIMEOUT_AGGREGATE",
            )
            await repo.update_status(document_id, from_status="PENDING", to_status="FAILED")
            return
        except Exception as exc:
            cause = exc.__cause__ if isinstance(exc.__cause__, IngestStepError) else None
            error_code = cause.error_code if cause is not None else "PIPELINE_TIMEOUT"
            log_ingest_step.failed(
                document_id=document_id,
                reason=f"{type(exc).__name__}: {exc}",
                error_code=error_code,
            )
            observe_pipeline_duration(
                source_app=doc.source_app,
                mime_type=doc.mime_type,
                seconds=time.monotonic() - started,
            )
            await repo.update_status(document_id, from_status="PENDING", to_status="FAILED")
            record_pipeline_outcome(
                source_app=doc.source_app, mime_type=doc.mime_type, outcome="failed"
            )
            return

        elapsed = time.monotonic() - started
        log_ingest_step.ready(
            document_id=document_id,
            chunks_total=chunks_total,
            duration_ms_total=int(elapsed * 1000),
        )

    observe_pipeline_duration(source_app=doc.source_app, mime_type=doc.mime_type, seconds=elapsed)
    await repo.promote_to_ready_and_demote_siblings(
        document_id=document_id,
        source_id=doc.source_id,
        source_app=doc.source_app,
    )
    record_pipeline_outcome(source_app=doc.source_app, mime_type=doc.mime_type, outcome="success")

    # File-type ingests are caller-owned: never delete.
    if (doc.ingest_type or "inline") == "inline":
        with contextlib.suppress(Exception):
            registry.delete_object(site, doc.object_key)

    await container.registry.fan_out(document_id)


@broker.task("ingest.supersede")
async def ingest_supersede_task(survivor_id: str, source_id: str, source_app: str) -> None:
    """T3.2d — Supersede worker task (R3, S26)."""
    from ragent.bootstrap.composition import get_container
    from ragent.services.ingest_service import IngestService

    container = get_container()
    svc = IngestService(
        repo=container.doc_repo,
        storage=container.minio_registry,
        broker=container.registry,
        registry=container.registry,
    )

    await svc.supersede(survivor_id, source_id, source_app)
