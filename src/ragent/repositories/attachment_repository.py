"""T-CAT.8 — AttachmentRepository: async CRUD for `chat_attachments` +
`chat_attachment_artifacts` (docs/spec/chat_attachments.md §9).

CRUD only, no business logic (R3) — status-transition rules and the
upload/encrypt/persist orchestration live in `chat_attachment_service.py`
(T-CAT.11), not here.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


@dataclass
class AttachmentRow:
    attachment_id: str
    thread_id: str
    create_user: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # 014_chat_attachments.sql: persisted failure diagnostics.
    error_code: str | None = None
    error_reason: str | None = None

    @classmethod
    def from_mapping(cls, m: Any) -> AttachmentRow:
        return cls(
            attachment_id=m["attachment_id"],
            thread_id=m["thread_id"],
            create_user=m["create_user"],
            filename=m["filename"],
            mime_type=m["mime_type"],
            size_bytes=m["size_bytes"],
            status=m["status"],
            created_at=m["created_at"],
            updated_at=m["updated_at"],
            error_code=m.get("error_code"),
            error_reason=m.get("error_reason"),
        )


@dataclass
class ArtifactRow:
    attachment_id: str
    variant: str
    storage_key: str
    content_type: str
    char_count: int
    created_at: datetime.datetime

    @classmethod
    def from_mapping(cls, m: Any) -> ArtifactRow:
        return cls(
            attachment_id=m["attachment_id"],
            variant=m["variant"],
            storage_key=m["storage_key"],
            content_type=m["content_type"],
            char_count=m["char_count"],
            created_at=m["created_at"],
        )


class AttachmentRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_all(self, stmt: Any, params: dict | None = None) -> list[Any]:
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt, params or {})
            return result.mappings().all()

    async def _fetch_first(self, stmt: Any, params: dict | None = None) -> Any | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt, params or {})
            return result.mappings().first()

    async def _execute(self, stmt: Any, params: dict | None = None) -> Any:
        async with self._engine.begin() as conn:
            return await conn.execute(stmt, params or {})

    # ------------------------------------------------------------------
    # chat_attachments — CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        attachment_id: str,
        thread_id: str,
        create_user: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
    ) -> str:
        await self._execute(
            text(
                """
                INSERT INTO chat_attachments
                    (attachment_id, thread_id, create_user, filename, mime_type,
                     size_bytes, status, created_at, updated_at)
                VALUES
                    (:attachment_id, :thread_id, :create_user, :filename, :mime_type,
                     :size_bytes, 'UPLOADED', NOW(6), NOW(6))
                """
            ),
            {
                "attachment_id": attachment_id,
                "thread_id": thread_id,
                "create_user": create_user,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
            },
        )
        return attachment_id

    async def get(self, attachment_id: str, create_user: str | None = None) -> AttachmentRow | None:
        user_clause = " AND create_user = :create_user" if create_user else ""
        params: dict = {"id": attachment_id}
        if create_user:
            params["create_user"] = create_user
        row = await self._fetch_first(
            text(f"SELECT * FROM chat_attachments WHERE attachment_id = :id{user_clause}"),
            params,
        )
        return AttachmentRow.from_mapping(row) if row else None

    async def list_by_thread(
        self,
        thread_id: str,
        create_user: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[AttachmentRow]:
        where_sql = "thread_id = :thread_id"
        params: dict = {"thread_id": thread_id}
        if create_user:
            where_sql += " AND create_user = :create_user"
            params["create_user"] = create_user
        return await self._list_attachments(where_sql, params, after, limit)

    async def update_status(
        self,
        attachment_id: str,
        status: str,
        error_code: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        await self._execute(
            text(
                "UPDATE chat_attachments SET status = :status, updated_at = NOW(6),"
                " error_code = :error_code, error_reason = :error_reason"
                " WHERE attachment_id = :id"
            ),
            {
                "status": status,
                "id": attachment_id,
                "error_code": error_code,
                "error_reason": error_reason[:255] if error_reason else None,
            },
        )

    async def list_by_user(
        self,
        create_user: str,
        after: str | None = None,
        limit: int = 50,
    ) -> list[AttachmentRow]:
        return await self._list_attachments(
            "create_user = :create_user", {"create_user": create_user}, after, limit
        )

    async def _list_attachments(
        self, where_sql: str, params: dict, after: str | None, limit: int
    ) -> list[AttachmentRow]:
        cursor_clause = " AND attachment_id < :after" if after else ""
        params = {**params, "limit": limit}
        if after:
            params["after"] = after
        rows = await self._fetch_all(
            text(
                f"SELECT * FROM chat_attachments WHERE {where_sql}{cursor_clause}"
                " ORDER BY created_at DESC, attachment_id DESC LIMIT :limit"
            ),
            params,
        )
        return [AttachmentRow.from_mapping(r) for r in rows]

    async def delete(self, attachment_id: str) -> None:
        """Delete an attachment row and its artifacts (no physical FK, §00_rule.md)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM chat_attachment_artifacts WHERE attachment_id = :id"),
                {"id": attachment_id},
            )
            await conn.execute(
                text("DELETE FROM chat_attachments WHERE attachment_id = :id"),
                {"id": attachment_id},
            )

    async def claim_for_processing(self, attachment_id: str) -> AttachmentRow | None:
        """Pre-state SELECT + atomic conditional UPDATE, UPLOADED→PROCESSING.

        Mirrors `DocumentRepository._atomic_claim` (no `attempt` counter —
        out of scope for chat attachments). Returns the pre-state row on a
        successful claim, or `None` if the row is missing, already terminal,
        or a concurrent worker won the race.
        """
        async with self._engine.begin() as conn:
            pre = (
                (
                    await conn.execute(
                        text("SELECT * FROM chat_attachments WHERE attachment_id = :id"),
                        {"id": attachment_id},
                    )
                )
                .mappings()
                .first()
            )
            if pre is None or pre["status"] != "UPLOADED":
                return None
            update_result = await conn.execute(
                text(
                    "UPDATE chat_attachments SET status='PROCESSING', updated_at=NOW(6)"
                    " WHERE attachment_id=:id AND status='UPLOADED'"
                ),
                {"id": attachment_id},
            )
            if update_result.rowcount == 0:
                return None
            return AttachmentRow.from_mapping(pre)

    # ------------------------------------------------------------------
    # chat_attachment_artifacts — CRUD
    # ------------------------------------------------------------------

    async def add_artifact(
        self,
        attachment_id: str,
        variant: str,
        storage_key: str,
        content_type: str,
        char_count: int,
    ) -> None:
        await self._execute(
            text(
                """
                INSERT INTO chat_attachment_artifacts
                    (attachment_id, variant, storage_key, content_type, char_count, created_at)
                VALUES
                    (:attachment_id, :variant, :storage_key, :content_type, :char_count, NOW(6))
                """
            ),
            {
                "attachment_id": attachment_id,
                "variant": variant,
                "storage_key": storage_key,
                "content_type": content_type,
                "char_count": char_count,
            },
        )

    async def get_artifacts(self, attachment_id: str) -> list[ArtifactRow]:
        rows = await self._fetch_all(
            text("SELECT * FROM chat_attachment_artifacts WHERE attachment_id = :id"),
            {"id": attachment_id},
        )
        return [ArtifactRow.from_mapping(r) for r in rows]
