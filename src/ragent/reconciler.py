"""T5.2 — Reconciler: one-shot stale-document recovery (B9, B16, S2, S3, S24, S26, S30).

Run via:  python -m ragent.reconciler
Scheduled as K8s CronJob (*/5 * * * *, concurrencyPolicy: Forbid).
"""

from __future__ import annotations

import asyncio
import datetime
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class Reconciler:
    def __init__(
        self,
        repo: Any,
        broker: Any,
        chunks: Any = None,
        registry: Any = None,
    ) -> None:
        self._repo = repo
        self._broker = broker
        self._chunks = chunks
        self._registry = registry

    def run(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        from ragent.bootstrap.telemetry import reconciler_tick_total

        await self._mark_failed()
        await self._redispatch_pending()
        await self._redispatch_uploaded()
        await self._resume_deleting()
        await self._repair_multi_ready()
        reconciler_tick_total.inc()
        logger.info("reconciler.tick")

    async def _mark_failed(self) -> None:
        max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "5"))
        exceeded = self._repo.list_pending_exceeded(attempt_gt=max_attempts)
        for doc in exceeded:
            try:
                # Commit terminal status first (Rule 21), then best-effort cleanup
                self._repo.update_status(doc.document_id, from_status="PENDING", to_status="FAILED")
                if self._registry is not None:
                    await self._registry.fan_out_delete(doc.document_id)
                if self._chunks is not None:
                    self._chunks.delete_by_document_id(doc.document_id)
                logger.info(
                    "ingest.failed",
                    document_id=doc.document_id,
                    attempt=doc.attempt,
                    reason="max_attempts_exceeded",
                )
            except Exception:
                logger.exception("reconciler.mark_failed_error", document_id=doc.document_id)

    async def _redispatch_pending(self) -> None:
        stale_seconds = int(os.environ.get("RECONCILER_PENDING_STALE_SECONDS", "300"))
        max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "5"))
        updated_before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = self._repo.list_pending_stale(
            updated_before=updated_before,
            attempt_le=max_attempts,
        )
        for doc in stale:
            await self._broker.enqueue("ingest.pipeline", document_id=doc.document_id)
            logger.info(
                "reconciler.redispatch",
                document_id=doc.document_id,
                attempt=doc.attempt,
            )

    async def _redispatch_uploaded(self) -> None:
        stale_seconds = int(os.environ.get("RECONCILER_UPLOADED_STALE_SECONDS", "300"))
        updated_before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = self._repo.list_uploaded_stale(updated_before=updated_before)
        for doc in stale:
            await self._broker.enqueue("ingest.pipeline", document_id=doc.document_id)
            logger.info(
                "reconciler.uploaded_redispatch",
                document_id=doc.document_id,
            )

    async def _resume_deleting(self) -> None:
        stale_seconds = int(os.environ.get("RECONCILER_DELETING_STALE_SECONDS", "300"))
        updated_before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = self._repo.list_deleting_stale(updated_before=updated_before)
        for doc in stale:
            try:
                if self._registry is not None:
                    await self._registry.fan_out_delete(doc.document_id)
                if self._chunks is not None:
                    self._chunks.delete_by_document_id(doc.document_id)
                self._repo.delete(doc.document_id)
                logger.info("reconciler.delete_resumed", document_id=doc.document_id)
            except Exception:
                logger.exception("reconciler.delete_resume_error", document_id=doc.document_id)

    async def _repair_multi_ready(self) -> None:
        groups = self._repo.find_multi_ready_groups()
        for source_id, source_app in groups:
            docs = self._repo.list_ready_by_source(source_id=source_id, source_app=source_app)
            if not docs:
                continue
            # Survivor is the doc with the latest created_at (last in ASC-ordered list)
            survivor = docs[-1]  # list_ready_by_source returns ASC by created_at; last is newest
            await self._broker.enqueue(
                "ingest.supersede",
                survivor_id=survivor.document_id,
                source_id=source_id,
                source_app=source_app,
            )
            logger.info(
                "reconciler.multi_ready_repair",
                source_id=source_id,
                source_app=source_app,
                survivor_id=survivor.document_id,
            )


def _build_from_env() -> Reconciler:
    # Importing the workers module triggers `@broker.task` registration
    # so dispatcher.enqueue() can resolve task labels (B25).
    import ragent.workers.ingest  # noqa: F401
    from ragent.bootstrap.broker import broker as taskiq_broker
    from ragent.bootstrap.composition import get_container
    from ragent.bootstrap.dispatcher import TaskiqDispatcher

    container = get_container()
    return Reconciler(
        repo=container.doc_repo,
        broker=TaskiqDispatcher(taskiq_broker),
        chunks=container.chunk_repo,
        registry=container.registry,
    )


async def _main_async() -> None:
    """Producer-side broker startup/shutdown around the tick (B27)."""
    from ragent.bootstrap.broker import broker as taskiq_broker

    await taskiq_broker.startup()
    try:
        await _build_from_env()._run_async()
    finally:
        await taskiq_broker.shutdown()


if __name__ == "__main__":
    from ragent.bootstrap.logging_config import configure_logging

    configure_logging("ragent-reconciler")
    asyncio.run(_main_async())
