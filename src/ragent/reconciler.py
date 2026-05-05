"""T5.2 — Reconciler: one-shot stale-document recovery (B9, B16, S2, S3, S24, S26, S30).

Run via:  python -m ragent.reconciler
Scheduled as K8s CronJob (*/5 * * * *, concurrencyPolicy: Forbid).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


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
        from ragent.bootstrap.telemetry import reconciler_tick_total

        self._mark_failed()
        self._redispatch_pending()
        self._redispatch_uploaded()
        self._resume_deleting()
        self._repair_multi_ready()
        reconciler_tick_total.inc()
        logger.info("event=reconciler.tick")

    def _mark_failed(self) -> None:
        max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "5"))
        exceeded = self._repo.list_pending_exceeded(attempt_gt=max_attempts)
        for doc in exceeded:
            # Clean partial output before committing FAILED (S27, R5)
            if self._registry is not None:
                asyncio.run(self._registry.fan_out_delete(doc.document_id))
            if self._chunks is not None:
                self._chunks.delete_by_document_id(doc.document_id)
            self._repo.update_status(doc.document_id, from_status="PENDING", to_status="FAILED")
            logger.info(
                "event=ingest.failed document_id=%s attempt=%d reason=max_attempts_exceeded",
                doc.document_id,
                doc.attempt,
            )

    def _redispatch_pending(self) -> None:
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
            self._broker.enqueue("ingest.pipeline", document_id=doc.document_id)
            logger.info(
                "event=reconciler.redispatch document_id=%s attempt=%d",
                doc.document_id,
                doc.attempt,
            )

    def _redispatch_uploaded(self) -> None:
        stale_seconds = int(os.environ.get("RECONCILER_UPLOADED_STALE_SECONDS", "300"))
        updated_before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = self._repo.list_uploaded_stale(updated_before=updated_before)
        for doc in stale:
            self._broker.enqueue("ingest.pipeline", document_id=doc.document_id)
            logger.info(
                "event=reconciler.uploaded_redispatch document_id=%s",
                doc.document_id,
            )

    def _resume_deleting(self) -> None:
        stale_seconds = int(os.environ.get("RECONCILER_DELETING_STALE_SECONDS", "300"))
        updated_before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = self._repo.list_deleting_stale(updated_before=updated_before)
        for doc in stale:
            if self._registry is not None:
                asyncio.run(self._registry.fan_out_delete(doc.document_id))
            if self._chunks is not None:
                self._chunks.delete_by_document_id(doc.document_id)
            self._repo.delete(doc.document_id)
            logger.info("event=reconciler.delete_resumed document_id=%s", doc.document_id)

    def _repair_multi_ready(self) -> None:
        groups = self._repo.find_multi_ready_groups()
        for source_id, source_app in groups:
            docs = self._repo.list_ready_by_source(source_id=source_id, source_app=source_app)
            if not docs:
                continue
            # Survivor is the doc with the latest created_at (last in ASC-ordered list)
            survivor = max(docs, key=lambda d: d.created_at)
            self._broker.enqueue(
                "ingest.supersede",
                survivor_id=survivor.document_id,
                source_id=source_id,
                source_app=source_app,
            )
            logger.info(
                "event=reconciler.multi_ready_repair source_id=%s source_app=%s survivor_id=%s",
                source_id,
                source_app,
                survivor.document_id,
            )


def _build_from_env() -> Reconciler:
    from ragent.bootstrap.broker import broker as taskiq_broker
    from ragent.bootstrap.composition import get_container

    container = get_container()
    return Reconciler(
        repo=container.doc_repo,
        broker=taskiq_broker,
        chunks=container.chunk_repo,
        registry=container.registry,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _build_from_env().run()
