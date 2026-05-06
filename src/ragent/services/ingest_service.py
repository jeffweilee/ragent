"""T2.8/T2.10/T2.12 / TA.4 — IngestService: async create / delete / list (spec §3.1, §4.1)."""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from typing import Any

from ragent.repositories.document_repository import LockNotAvailable
from ragent.utility.id_gen import new_id

_ALLOWED_MIMES = frozenset({"text/plain", "text/markdown", "text/html", "text/csv"})
_MAX_FILE_SIZE = int(os.environ.get("INGEST_MAX_FILE_SIZE_BYTES", "52428800"))
_LIST_MAX = int(os.environ.get("INGEST_LIST_MAX_LIMIT", "100"))


class MimeNotAllowed(Exception):
    pass


class FileTooLarge(Exception):
    pass


class DocumentNotFound(Exception):
    pass


@dataclass
class IngestListResult:
    items: list[Any]
    next_cursor: str | None


class IngestService:
    def __init__(self, repo: Any, chunks: Any, storage: Any, broker: Any) -> None:
        self._repo = repo
        self._chunks = chunks
        self._storage = storage
        self._broker = broker
        self._has_fan_out = hasattr(broker, "fan_out_delete")

    # ------------------------------------------------------------------
    # Create (T2.8)
    # ------------------------------------------------------------------

    async def create(
        self,
        create_user: str,
        source_id: str,
        source_app: str,
        source_title: str,
        file_data: io.IOBase,
        file_size: int,
        content_type: str,
        source_workspace: str | None = None,
        max_file_size: int | None = None,
    ) -> str:
        limit = max_file_size if max_file_size is not None else _MAX_FILE_SIZE
        if file_size > limit:
            raise FileTooLarge(f"File size {file_size} exceeds {limit}")
        if content_type not in _ALLOWED_MIMES:
            raise MimeNotAllowed(f"MIME {content_type!r} not in allow-list")

        document_id = new_id()
        object_key = self._storage.put_object(
            source_app=source_app,
            source_id=source_id,
            document_id=document_id,
            data=file_data,
            length=file_size,
            content_type=content_type,
        )
        await self._repo.create(
            document_id=document_id,
            create_user=create_user,
            source_id=source_id,
            source_app=source_app,
            source_title=source_title,
            source_workspace=source_workspace,
            object_key=object_key,
        )
        await self._broker.enqueue("ingest.pipeline", document_id=document_id)
        return document_id

    # ------------------------------------------------------------------
    # Get (used by router)
    # ------------------------------------------------------------------

    async def get(self, document_id: str) -> Any | None:
        return await self._repo.get(document_id)

    # ------------------------------------------------------------------
    # Delete (T2.10)
    # ------------------------------------------------------------------

    async def delete(self, document_id: str) -> None:
        try:
            doc = await self._repo.claim_for_deletion(document_id)
        except LockNotAvailable:
            return  # not found or contended — both are idempotent 204

        # Outside any DB tx: fan_out_delete → chunk delete → MinIO (if staged)
        if self._has_fan_out:
            self._broker.fan_out_delete(document_id)

        await self._chunks.delete_by_document_id(document_id)

        if doc.status in ("UPLOADED", "PENDING"):
            with contextlib.suppress(Exception):
                self._storage.delete_object(doc.object_key)

        await self._repo.delete(document_id)

    # ------------------------------------------------------------------
    # Supersede (T3.2d)
    # ------------------------------------------------------------------

    async def supersede(self, survivor_id: str, source_id: str, source_app: str) -> None:
        while True:
            loser = await self._repo.pop_oldest_loser_for_supersede(
                source_id, source_app, survivor_id
            )
            if loser is None:
                break
            await self._chunks.delete_by_document_id(loser.document_id)
            await self._repo.delete(loser.document_id)

    # ------------------------------------------------------------------
    # List (T2.12)
    # ------------------------------------------------------------------

    async def list(self, after: str | None = None, limit: int = _LIST_MAX) -> IngestListResult:
        limit = min(limit, _LIST_MAX)
        rows = await self._repo.list(after=after, limit=limit + 1)
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = items[-1].document_id if has_more and items else None
        return IngestListResult(items=items, next_cursor=next_cursor)
