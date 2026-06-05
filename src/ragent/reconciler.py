"""T5.2 / TA.8 — Reconciler: one-shot stale-document recovery (B9, B16, S2, S3, S24, S26, S30).

Run via:  python -m ragent.reconciler
Scheduled as K8s CronJob (*/5 * * * *, concurrencyPolicy: Forbid).

All methods are async; the sync entrypoint wraps via asyncio.run().
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import os
from typing import Any

import structlog
from elasticsearch import NotFoundError
from sqlalchemy.ext.asyncio import create_async_engine

from ragent.errors.codes import TaskErrorCode
from ragent.repositories.document_repository import DocumentRepository

logger = structlog.get_logger(__name__)


class Reconciler:
    def __init__(
        self,
        repo: Any,
        broker: Any,
        registry: Any = None,
        *,
        settings_repo: Any = None,
        es_client: Any = None,
        chunks_index: str = "chunks_v1",
    ) -> None:
        self._repo = repo
        self._broker = broker
        self._registry = registry
        self._settings_repo = settings_repo
        self._es_client = es_client
        self._chunks_index = chunks_index

    def run(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        from ragent.bootstrap.metrics import reconciler_tick_total

        await self._mark_failed()
        await self._redispatch_pending()
        await self._redispatch_uploaded()
        await self._resume_deleting()
        await self._repair_multi_ready()
        await self._backfill_candidate_embeddings()
        await self._sweep_retired_embedding_indices()
        reconciler_tick_total.inc()
        logger.info("reconciler.tick")

    async def _mark_failed(self) -> None:
        max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "5"))
        exceeded = await self._repo.list_pending_exceeded(attempt_gt=max_attempts)
        for doc in exceeded:
            try:
                # Commit terminal status first (Rule 21), then best-effort cleanup.
                # Persist error_code so GET /ingest/{id} on a reconciler-driven
                # FAIL exposes the same diagnostic shape as a worker-driven FAIL.
                await self._repo.update_status(
                    doc.document_id,
                    from_status="PENDING",
                    to_status="FAILED",
                    error_code=TaskErrorCode.PIPELINE_MAX_ATTEMPTS_EXCEEDED,
                    error_reason=f"reconciler swept stuck PENDING after attempt={doc.attempt}",
                )
                from ragent.bootstrap.metrics import record_pipeline_outcome

                record_pipeline_outcome(
                    source_app=doc.source_app,
                    mime_type=doc.mime_type,
                    outcome="failed",
                )
                if self._registry is not None:
                    await self._registry.fan_out_delete(doc.document_id)
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
        updated_before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = await self._repo.list_pending_stale(
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
        updated_before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = await self._repo.list_uploaded_stale(updated_before=updated_before)
        for doc in stale:
            await self._broker.enqueue("ingest.pipeline", document_id=doc.document_id)
            logger.info(
                "reconciler.uploaded_redispatch",
                document_id=doc.document_id,
            )

    async def _resume_deleting(self) -> None:
        stale_seconds = int(os.environ.get("RECONCILER_DELETING_STALE_SECONDS", "300"))
        updated_before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=stale_seconds
        )
        stale = await self._repo.list_deleting_stale(updated_before=updated_before)
        for doc in stale:
            try:
                if self._registry is not None:
                    await self._registry.fan_out_delete(doc.document_id)
                await self._repo.delete(doc.document_id)
                logger.info("reconciler.delete_resumed", document_id=doc.document_id)
            except Exception:
                logger.exception("reconciler.delete_resume_error", document_id=doc.document_id)

    async def _backfill_candidate_embeddings(self) -> None:
        """T-EM-R.9 — enqueue backfill task when candidate coverage < 0.99.

        Reads embedding.candidate; if an index_name is set (CANDIDATE/CUTOVER
        state), compares doc counts. Enqueues ingest.backfill_candidate when
        ratio < 0.99. No-ops when IDLE or when settings/es are not wired.
        """
        if self._settings_repo is None or self._es_client is None:
            return
        try:
            settings = await self._settings_repo.get_many(
                ["embedding.stable", "embedding.candidate"]
            )
        except Exception:
            logger.exception("reconciler.backfill.read_error")
            return
        stable = settings.get("embedding.stable")
        candidate = settings.get("embedding.candidate")
        if not candidate or not candidate.get("index_name"):
            return

        candidate_index = candidate["index_name"]
        stable_index = (stable or {}).get("index_name") or self._chunks_index
        try:
            stable_resp, candidate_resp = await asyncio.gather(
                self._es_client.count(index=stable_index),
                self._es_client.count(index=candidate_index),
            )
            stable_count = stable_resp["count"]
            if stable_count == 0:
                return
            candidate_count = candidate_resp["count"]
            if candidate_count / stable_count >= 0.99:
                return
            await self._broker.enqueue(
                "ingest.backfill_candidate",
                stable_index=stable_index,
                candidate_index=candidate_index,
            )
            logger.info(
                "reconciler.backfill_enqueued",
                ratio=candidate_count / stable_count,
            )
        except Exception:
            logger.exception("reconciler.backfill.es_error")

    async def _sweep_retired_embedding_indices(self) -> None:
        """T-EM-R.8 — delete physical ES indices for retired embedding models.

        For each ``embedding.retired`` entry with ``cleanup_done=false`` and
        ``index_name`` set, calls DELETE /index_name (404 treated as success),
        then marks the entry done via an optimistic-locked transition. Legacy
        entries without ``index_name`` are skipped silently. Errors on a single
        entry do not abort the sweep for remaining entries.
        """
        if self._settings_repo is None or self._es_client is None:
            return
        try:
            retired = await self._settings_repo.get("embedding.retired") or []
        except Exception:
            logger.exception("reconciler.retired_sweep.read_error")
            return
        if not retired:
            return
        pending = [e for e in retired if not e.get("cleanup_done") and e.get("index_name")]
        if not pending:
            return

        original = copy.deepcopy(retired)
        for entry in pending:
            index_name = entry["index_name"]
            try:
                await self._es_client.indices.delete(index=index_name)
                entry["cleanup_done"] = True
                logger.info("reconciler.retired_index_deleted", index_name=index_name)
            except NotFoundError:
                entry["cleanup_done"] = True
                logger.info("reconciler.retired_index_already_gone", index_name=index_name)
            except Exception:
                logger.exception("reconciler.retired_sweep.es_error", index_name=index_name)

        if not any(e.get("cleanup_done") for e in pending):
            return
        try:
            await self._settings_repo.transition(
                {"embedding.retired": retired},
                expect={"embedding.retired": original},
            )
        except Exception:
            logger.exception("reconciler.retired_sweep.transition_error")

    async def _repair_multi_ready(self) -> None:
        groups = await self._repo.find_multi_ready_groups()
        for source_id, source_app in groups:
            docs = await self._repo.list_ready_by_source(source_id=source_id, source_app=source_app)
            if not docs:
                continue
            # list_ready_by_source returns ASC by created_at; last is newest
            survivor = docs[-1]
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


class _PerTickRunner:
    """T7.4.x(a) — Build the AsyncEngine + repo per tick.

    ``Reconciler.run()`` calls ``asyncio.run()``, which closes the loop on
    exit. SQLAlchemy ``AsyncEngine`` instances bind to the loop on first
    use, so a long-running poller (chaos drill in T7.4) that calls
    ``run()`` repeatedly cannot share a single engine. Building inside the
    tick's loop and disposing on exit keeps each tick self-contained.
    """

    def run(self) -> None:
        asyncio.run(self._tick())

    async def _tick(self) -> None:
        from ragent.bootstrap.broker import broker as taskiq_broker
        from ragent.bootstrap.composition import get_container
        from ragent.bootstrap.dispatcher import TaskiqDispatcher
        from ragent.bootstrap.init_schema import patch_aiomysql_ping, to_async_dsn
        from ragent.utility.env import int_env

        engine = create_async_engine(
            to_async_dsn(os.environ["MARIADB_DSN"]),
            pool_pre_ping=True,
            pool_recycle=int_env("MARIADB_POOL_RECYCLE_SECONDS", 280),
        )
        patch_aiomysql_ping(engine)
        try:
            await taskiq_broker.startup()
            try:
                container = get_container()
                await container.embedding_registry.refresh()
                rec = Reconciler(
                    repo=DocumentRepository(engine=engine),
                    broker=TaskiqDispatcher(taskiq_broker),
                    registry=container.registry,
                    settings_repo=container.system_settings_repo,
                    es_client=container.es_client,
                    chunks_index=container.chunks_index_name,
                )
                await rec._run_async()
            finally:
                await taskiq_broker.shutdown()
        finally:
            await engine.dispose()


def _build_from_env() -> _PerTickRunner:
    # Importing the workers modules triggers `@broker.task` registration
    # so dispatcher.enqueue() can resolve task labels (B25).
    import ragent.workers.backfill  # noqa: F401
    import ragent.workers.ingest  # noqa: F401

    return _PerTickRunner()


if __name__ == "__main__":
    from ragent.bootstrap.logging_config import configure_logging

    configure_logging("ragent-reconciler")
    _build_from_env().run()
