"""T2v.27 — IngestService v2: discriminated dispatch (inline | file).

Inline path stages bytes to the `__default__` MinIO site under a server-built
object key; file path records the caller's `(minio_site, object_key)` after a
HEAD probe and never copies. Cleanup branches on `documents.ingest_type` and
the site's `read_only` flag.
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from typing import Any

import structlog

from ragent.schemas.ingest import (
    SOURCE_URL_MAX,
    FileIngestRequest,
    IngestMime,
    InlineIngestRequest,
)
from ragent.storage.minio_registry import UnknownMinioSite
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)

_MAX_INLINE_BYTES = int(os.environ.get("INGEST_INLINE_MAX_BYTES", "52428800"))
_MAX_FILE_BYTES = int(os.environ.get("INGEST_FILE_MAX_BYTES", "52428800"))
_LIST_MAX = int(os.environ.get("INGEST_LIST_MAX_LIMIT", "100"))


class MimeNotAllowed(Exception):
    pass


class FileTooLarge(Exception):
    pass


class DocumentNotFound(Exception):
    pass


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
    ) -> None:
        self._repo = repo
        self._storage = storage  # MinioSiteRegistry
        self._broker = broker  # TaskiqDispatcher (create); unused in supersede path
        self._registry = registry

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
        await self._broker.enqueue("ingest.pipeline", document_id=document_id)
        logger.info(
            "ingest.dispatched",
            document_id=document_id,
            source_id=request.source_id,
            source_app=request.source_app,
            task_label="ingest.pipeline",
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
        limit = max_bytes if max_bytes is not None else _MAX_INLINE_BYTES
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
        limit = max_file_bytes if max_file_bytes is not None else _MAX_FILE_BYTES
        if size is not None and size > limit:
            raise FileTooLarge(f"File {size}B exceeds limit {limit}B")
        return request.object_key, request.minio_site

    async def get(self, document_id: str) -> Any | None:
        return await self._repo.get(document_id)

    async def delete(self, document_id: str) -> None:
        doc = await self._repo.claim_for_deletion(document_id)
        if doc is None:
            return

        # Cascade plugin cleanup (ES chunks, etc.) before the DB row is
        # hard-deleted. Hydrator drop keeps any straggler chunks invisible
        # to /chat between this call and reconciler reclaim.
        await self._registry.fan_out_delete(document_id)

        if doc.status in ("UPLOADED", "PENDING"):
            with contextlib.suppress(Exception):
                self._delete_object(doc)

        await self._repo.delete(document_id)
        logger.info(
            "ingest.deleted",
            document_id=document_id,
            source_id=getattr(doc, "source_id", None),
            source_app=getattr(doc, "source_app", None),
            prior_status=getattr(doc, "status", None),
        )

    def _delete_object(self, doc: Any) -> None:
        ingest_type = getattr(doc, "ingest_type", "inline")
        if ingest_type == "file":
            return  # caller owns the bytes
        site = getattr(doc, "minio_site", None)
        if hasattr(self._storage, "delete_object"):
            try:
                if site:
                    self._storage.delete_object(site, doc.object_key)
                else:
                    # v1 MinIOClient takes only object_key; v2 registry requires
                    # (site, key). TypeError from v2 is the disambiguator.
                    try:
                        self._storage.delete_object(doc.object_key)
                    except TypeError:
                        self._storage.delete_object("__default__", doc.object_key)
            except UnknownMinioSite:
                return

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
            ingest_type="inline",
            minio_site=None,
            mime_type=mime_type.value,
        )
        await self._broker.enqueue("ingest.pipeline", document_id=document_id)
        logger.info(
            "ingest.dispatched",
            document_id=document_id,
            source_id=source_id,
            source_app=source_app,
            task_label="ingest.pipeline",
        )
        logger.info(
            "ingest.received",
            document_id=document_id,
            ingest_type="inline",
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
        limit: int = _LIST_MAX,
        source_id: str | None = None,
        source_app: str | None = None,
    ) -> IngestListResult:
        limit = min(limit, _LIST_MAX)
        rows = await self._repo.list(
            after=after, limit=limit + 1, source_id=source_id, source_app=source_app
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = items[-1].document_id if has_more and items else None
        return IngestListResult(items=items, next_cursor=next_cursor)
