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

from ragent.repositories.document_repository import LockNotAvailable
from ragent.schemas.ingest import (
    SOURCE_URL_MAX,
    FileIngestRequest,
    InlineIngestRequest,
)
from ragent.storage.minio_registry import UnknownMinioSite
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)

_MAX_INLINE_BYTES = int(os.environ.get("INGEST_INLINE_MAX_BYTES", "52428800"))
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
    def __init__(self, repo: Any, storage: Any, broker: Any) -> None:
        self._repo = repo
        self._storage = storage  # MinioSiteRegistry (v2) or legacy stub
        self._broker = broker
        self._has_fan_out = hasattr(broker, "fan_out_delete")

    async def create(
        self,
        *,
        create_user: str,
        request: InlineIngestRequest | FileIngestRequest,
        max_inline_bytes: int | None = None,
    ) -> str:
        document_id = new_id()
        if isinstance(request, InlineIngestRequest):
            object_key, minio_site = self._stage_inline(request, document_id, max_inline_bytes)
            ingest_type = "inline"
        else:
            object_key, minio_site = self._record_file(request)
            ingest_type = "file"

        await self._repo.create(
            document_id=document_id,
            create_user=create_user,
            source_id=request.source_id,
            source_app=request.source_app,
            source_title=request.source_title,
            source_workspace=request.source_workspace,
            source_url=request.source_url,
            object_key=object_key,
            ingest_type=ingest_type,
            minio_site=minio_site,
        )
        await self._broker.enqueue("ingest.pipeline", document_id=document_id)
        logger.info(
            "ingest.received",
            document_id=document_id,
            ingest_type=ingest_type,
            mime_type=request.mime_type.value,
            source_id=request.source_id,
            source_app=request.source_app,
        )
        return document_id

    def _stage_inline(
        self,
        request: InlineIngestRequest,
        document_id: str,
        max_inline_bytes: int | None,
    ) -> tuple[str, str | None]:
        limit = max_inline_bytes if max_inline_bytes is not None else _MAX_INLINE_BYTES
        data = request.content.encode("utf-8")
        if len(data) > limit:
            raise FileTooLarge(f"Inline content {len(data)}B exceeds {limit}B")
        if request.source_url and len(request.source_url) > SOURCE_URL_MAX:
            raise ValueError("source_url too long")
        object_key = self._storage.put_object_default(
            source_app=request.source_app,
            source_id=request.source_id,
            document_id=document_id,
            data=io.BytesIO(data),
            length=len(data),
            content_type=request.mime_type.value,
        )
        return object_key, None

    def _record_file(self, request: FileIngestRequest) -> tuple[str, str]:
        try:
            self._storage.get(request.minio_site)
        except UnknownMinioSite as exc:
            raise UnknownMinioSiteError(request.minio_site) from exc
        size = self._storage.stat_object(request.minio_site, request.object_key)
        if size is None:
            raise ObjectNotFoundError(f"{request.minio_site}/{request.object_key} not found")
        return request.object_key, request.minio_site

    async def get(self, document_id: str) -> Any | None:
        return await self._repo.get(document_id)

    async def delete(self, document_id: str) -> None:
        try:
            doc = await self._repo.claim_for_deletion(document_id)
        except LockNotAvailable:
            return

        if self._has_fan_out:
            self._broker.fan_out_delete(document_id)

        if doc.status in ("UPLOADED", "PENDING"):
            with contextlib.suppress(Exception):
                self._delete_object(doc)

        await self._repo.delete(document_id)

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
                    # Legacy MinIOClient signature OR v2 registry default site.
                    try:
                        self._storage.delete_object(doc.object_key)
                    except TypeError:
                        self._storage.delete_object("__default__", doc.object_key)
            except UnknownMinioSite:
                return

    async def supersede(self, survivor_id: str, source_id: str, source_app: str) -> None:
        while True:
            loser = await self._repo.pop_oldest_loser_for_supersede(
                source_id, source_app, survivor_id
            )
            if loser is None:
                break
            await self._repo.delete(loser.document_id)

    async def list(self, after: str | None = None, limit: int = _LIST_MAX) -> IngestListResult:
        limit = min(limit, _LIST_MAX)
        rows = await self._repo.list(after=after, limit=limit + 1)
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = items[-1].document_id if has_more and items else None
        return IngestListResult(items=items, next_cursor=next_cursor)
