"""T2v.27 — IngestService v2: discriminated dispatch (inline | file).

Inline path stages bytes to the `__default__` MinIO site under a server-built
object key; file path records the caller's `(minio_site, object_key)` after a
HEAD probe and never copies. MinIO objects are retained for audit/replay;
delete and supersede cleanup only derived stores such as ES chunks.
"""

from __future__ import annotations

import datetime
import io
from dataclasses import dataclass
from typing import Any

import structlog

from ragent.bootstrap.dispatcher import TaskDispatchUnavailable
from ragent.bootstrap.metrics import ingest_dispatch_deferred_total
from ragent.schemas.ingest import (
    SOURCE_URL_MAX,
    FileIngestRequest,
    IngestMime,
    InlineIngestRequest,
)
from ragent.storage.minio_registry import UnknownMinioSite
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)

# Spec §4.6 defaults; composition.py reads INGEST_*_MAX_BYTES / _LIMIT env vars
# and overrides via constructor kwargs. Kept here as numeric constants — not env
# reads — so tests can construct IngestService without setting env.
INLINE_MAX_BYTES_DEFAULT = 10 * 1024 * 1024
FILE_MAX_BYTES_DEFAULT = 50 * 1024 * 1024
LIST_MAX_LIMIT_DEFAULT = 100


class MimeNotAllowed(Exception):
    pass


class FileTooLarge(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class DocumentNotRerunnable(Exception):
    """Rerun requested on a document whose status is READY or DELETING."""


class UnknownMinioSiteError(Exception):
    pass


class ObjectNotFoundError(Exception):
    pass


@dataclass
class IngestListResult:
    items: list[Any]
    next_cursor: str | None


class IngestService:
    def __init__(
        self,
        repo: Any,
        storage: Any,
        broker: Any,
        registry: Any,
        *,
        inline_max_bytes: int = INLINE_MAX_BYTES_DEFAULT,
        file_max_bytes: int = FILE_MAX_BYTES_DEFAULT,
        list_max_limit: int = LIST_MAX_LIMIT_DEFAULT,
    ) -> None:
        self._repo = repo
        self._storage = storage  # MinioSiteRegistry
        self._broker = broker  # TaskiqDispatcher (create); unused in supersede path
        self._registry = registry
        self._inline_max_bytes = inline_max_bytes
        self._file_max_bytes = file_max_bytes
        self._list_max_limit = list_max_limit

    async def create(
        self,
        *,
        create_user: str,
        request: InlineIngestRequest | FileIngestRequest,
        max_inline_bytes: int | None = None,
        max_file_bytes: int | None = None,
    ) -> str:
        document_id = new_id()
        if isinstance(request, InlineIngestRequest):
            object_key, minio_site = self._stage_inline(request, document_id, max_inline_bytes)
            ingest_type = "inline"
        else:
            object_key, minio_site = self._record_file(request, max_file_bytes)
            ingest_type = "file"

        await self._repo.create(
            document_id=document_id,
            create_user=create_user,
            source_id=request.source_id,
            source_app=request.source_app,
            source_title=request.source_title,
            source_meta=request.source_meta,
            source_url=request.source_url,
            object_key=object_key,
            ingest_type=ingest_type,
            minio_site=minio_site,
            mime_type=request.mime_type.value,
        )
        await self._dispatch(
            document_id, source_id=request.source_id, source_app=request.source_app
        )
        logger.info(
            "ingest.received",
            document_id=document_id,
            ingest_type=ingest_type,
            mime_type=request.mime_type.value,
            source_id=request.source_id,
            source_app=request.source_app,
        )
        return document_id

    async def _dispatch(
        self, document_id: str, *, event: str = "ingest.dispatched", **log_fields: Any
    ) -> None:
        """Enqueue the pipeline; a queue outage defers rather than fails.

        Every caller has already persisted the row in a re-dispatchable state
        (UPLOADED from `create`/`create_from_upload`, or reset by
        `mark_for_rerun`), and the worker's `run_startup_sweep` /
        `run_maintenance_cycle` pick those rows up once Redis returns. So the
        work is not lost and the request keeps its 202 — reporting a 500 would
        tell the caller the upload failed when it is durably stored and queued
        for the sweep.

        `TaskNotRegisteredError` deliberately still escapes: no sweep can heal a
        task that was never registered.
        """
        try:
            await self._broker.enqueue("ingest.pipeline", document_id=document_id)
        except TaskDispatchUnavailable as exc:
            ingest_dispatch_deferred_total.inc()
            logger.warning(
                "ingest.dispatch_deferred",
                document_id=document_id,
                task_label="ingest.pipeline",
                reason=str(exc),
                **log_fields,
            )
            return
        logger.info(event, document_id=document_id, task_label="ingest.pipeline", **log_fields)

    def _put_to_default_site(
        self,
        *,
        data: bytes,
        source_app: str,
        source_id: str,
        document_id: str,
        mime_type: str,
        max_bytes: int | None,
    ) -> str:
        limit = max_bytes if max_bytes is not None else self._inline_max_bytes
        data_len = len(data)
        if data_len > limit:
            raise FileTooLarge(f"Content {data_len}B exceeds limit {limit}B")
        return self._storage.put_object_default(
            source_app=source_app,
            source_id=source_id,
            document_id=document_id,
            data=io.BytesIO(data),
            length=data_len,
            content_type=mime_type,
        )

    def _stage_inline(
        self,
        request: InlineIngestRequest,
        document_id: str,
        max_inline_bytes: int | None,
    ) -> tuple[str, str | None]:
        data = request.content.encode("utf-8")
        if request.source_url and len(request.source_url) > SOURCE_URL_MAX:
            raise ValueError("source_url too long")
        object_key = self._put_to_default_site(
            data=data,
            source_app=request.source_app,
            source_id=request.source_id,
            document_id=document_id,
            mime_type=request.mime_type.value,
            max_bytes=max_inline_bytes,
        )
        return object_key, None

    def _record_file(
        self, request: FileIngestRequest, max_file_bytes: int | None = None
    ) -> tuple[str, str]:
        try:
            res = self._storage.head_object(request.minio_site, request.object_key)
        except UnknownMinioSite as exc:
            raise UnknownMinioSiteError(request.minio_site) from exc
        if res is None:
            raise ObjectNotFoundError(f"{request.minio_site}/{request.object_key} not found")
        size, _ = res
        limit = max_file_bytes if max_file_bytes is not None else self._file_max_bytes
        if size is not None and size > limit:
            raise FileTooLarge(f"File {size}B exceeds limit {limit}B")
        return request.object_key, request.minio_site

    async def get(self, document_id: str) -> Any | None:
        return await self._repo.get(document_id)

    async def rerun(self, document_id: str) -> None:
        """Manually re-dispatch the ingest pipeline for a non-READY document."""
        outcome = await self._repo.mark_for_rerun(document_id)
        if outcome == "not_found":
            raise DocumentNotFound(document_id)
        if outcome == "not_rerunnable":
            raise DocumentNotRerunnable(document_id)
        await self._dispatch(document_id, event="ingest.rerun_dispatched")

    async def batch_rerun(
        self,
        *,
        statuses: list[str],
        source_app: str | None = None,
        source_id: str | None = None,
        created_after: datetime.datetime | None = None,
        limit: int = 500,
        dry_run: bool = False,
        operator_id: str | None = None,
    ) -> tuple[dict[str, int], dict[str, int], int, int]:
        filter_kwargs = dict(
            source_app=source_app,
            source_id=source_id,
            created_after=created_after,
        )
        logger.info(
            "ingest.batch_rerun_started",
            statuses=statuses,
            **filter_kwargs,
            limit=limit,
            dry_run=dry_run,
            operator_id=operator_id,
        )
        before = await self._repo.count_by_statuses(statuses, **filter_kwargs)
        if dry_run:
            logger.info(
                "ingest.batch_rerun_preview",
                statuses=statuses,
                **filter_kwargs,
                before=before,
                operator_id=operator_id,
            )
            return before, before, 0, 0

        docs = await self._repo.list_by_statuses(statuses, **filter_kwargs, limit=limit)
        queued = skipped = 0
        for doc in docs:
            outcome = await self._repo.mark_for_rerun(doc.document_id)
            if outcome == "ok":
                await self._dispatch(
                    doc.document_id,
                    event="ingest.batch_rerun_item_dispatched",
                    operator_id=operator_id,
                )
                queued += 1
            else:
                skipped += 1

        after = await self._repo.count_by_statuses(statuses, **filter_kwargs)
        logger.info(
            "ingest.batch_rerun_dispatched",
            statuses=statuses,
            **filter_kwargs,
            limit=limit,
            before=before,
            after=after,
            queued=queued,
            skipped=skipped,
            operator_id=operator_id,
        )
        return before, after, queued, skipped

    async def delete(self, document_id: str) -> None:
        doc = await self._repo.claim_for_deletion(document_id)
        if doc is None:
            return

        # Cascade plugin cleanup (ES chunks, etc.) before the DB row is
        # hard-deleted. Hydrator drop keeps any straggler chunks invisible
        # to /chat between this call and reconciler reclaim.
        await self._registry.fan_out_delete(document_id)

        await self._repo.delete(document_id)
        logger.info(
            "ingest.deleted",
            document_id=document_id,
            source_id=getattr(doc, "source_id", None),
            source_app=getattr(doc, "source_app", None),
            prior_status=getattr(doc, "status", None),
        )

    async def create_from_upload(
        self,
        *,
        create_user: str,
        source_id: str,
        source_app: str,
        source_title: str,
        mime_type: IngestMime,
        data: bytes,
        source_meta: str | None = None,
        source_url: str | None = None,
        max_upload_bytes: int | None = None,
        persist_size_bytes: bool = False,
    ) -> str:
        document_id = new_id()
        object_key = self._put_to_default_site(
            data=data,
            source_app=source_app,
            source_id=source_id,
            document_id=document_id,
            mime_type=mime_type.value,
            max_bytes=max_upload_bytes,
        )
        await self._repo.create(
            document_id=document_id,
            create_user=create_user,
            source_id=source_id,
            source_app=source_app,
            source_title=source_title,
            object_key=object_key,
            source_meta=source_meta,
            source_url=source_url,
            ingest_type="upload",
            minio_site=None,
            mime_type=mime_type.value,
            # Chat-attachment uploads report AttachmentInfo.sizeBytes back to
            # the client; other upload callers keep the column NULL.
            size_bytes=len(data) if persist_size_bytes else None,
        )
        await self._dispatch(document_id, source_id=source_id, source_app=source_app)
        logger.info(
            "ingest.received",
            document_id=document_id,
            ingest_type="upload",
            mime_type=mime_type.value,
            source_id=source_id,
            source_app=source_app,
        )
        return document_id

    async def supersede(self, survivor_id: str, source_id: str, source_app: str) -> None:
        while True:
            loser = await self._repo.pop_oldest_loser_for_supersede(
                source_id, source_app, survivor_id
            )
            if loser is None:
                break
            # Cascade through self.delete so plugin stores (ES chunks, etc.)
            # drop the loser's data, not just the documents row.
            # Spec §3.1 line 92 — "cascade-delete that row".
            await self.delete(loser.document_id)

    async def list(
        self,
        after: str | None = None,
        limit: int | None = None,
        source_id: str | None = None,
        source_app: str | None = None,
    ) -> IngestListResult:
        cap = self._list_max_limit
        limit = min(limit if limit is not None else cap, cap)
        rows = await self._repo.list(
            after=after, limit=limit + 1, source_id=source_id, source_app=source_app
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = items[-1].document_id if has_more and items else None
        return IngestListResult(items=items, next_cursor=next_cursor)
