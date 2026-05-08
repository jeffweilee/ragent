"""T2.2 / TA.2 — DocumentRepository: async CRUD + locking (spec §5.1, B11, B14, B17).

Per `docs/00_rule.md` Database Practices: every method checks out a fresh
async connection from the engine's pool and releases it on exit. All methods
are `async def` for direct use in FastAPI routes and TaskIQ tasks.

Sync bridge for Haystack pipeline components (anyio threads): use
`anyio.from_thread.run(repo.method, *args)`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ragent.utility.state_machine import IllegalStateTransition, assert_transition


class LockNotAvailable(Exception):
    """Raised when FOR UPDATE NOWAIT finds a contended row (R7, S28)."""


@dataclass
class DocumentRow:
    document_id: str
    create_user: str
    source_id: str
    source_app: str
    source_title: str
    source_meta: str | None
    object_key: str
    status: str
    attempt: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # v2 columns (002_ingest_v2.sql). Defaults keep test fixtures green.
    ingest_type: str = "inline"
    minio_site: str | None = None
    source_url: str | None = None
    mime_type: str | None = None

    @classmethod
    def from_mapping(cls, m: Any) -> DocumentRow:
        return cls(
            document_id=m["document_id"],
            create_user=m["create_user"],
            source_id=m["source_id"],
            source_app=m["source_app"],
            source_title=m["source_title"],
            source_meta=m.get("source_meta"),
            object_key=m["object_key"],
            status=m["status"],
            attempt=m["attempt"],
            created_at=m["created_at"],
            updated_at=m["updated_at"],
            ingest_type=m.get("ingest_type") or "inline",
            minio_site=m.get("minio_site"),
            source_url=m.get("source_url"),
            mime_type=m.get("mime_type"),
        )


def _rows_to_docs(rows: Any) -> list[DocumentRow]:
    return [DocumentRow.from_mapping(r) for r in rows]


class DocumentRepository:
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
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        document_id: str,
        create_user: str,
        source_id: str,
        source_app: str,
        source_title: str,
        object_key: str,
        source_meta: str | None = None,
        source_url: str | None = None,
        ingest_type: str = "inline",
        minio_site: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        await self._execute(
            text(
                """
                INSERT INTO documents
                    (document_id, create_user, source_id, source_app, source_title,
                     source_meta, source_url, object_key, ingest_type, minio_site,
                     mime_type, status, attempt, created_at, updated_at)
                VALUES
                    (:document_id, :create_user, :source_id, :source_app, :source_title,
                     :source_meta, :source_url, :object_key, :ingest_type, :minio_site,
                     :mime_type, 'UPLOADED', 0, NOW(6), NOW(6))
                """
            ),
            {
                "document_id": document_id,
                "create_user": create_user,
                "source_id": source_id,
                "source_app": source_app,
                "source_title": source_title,
                "source_meta": source_meta,
                "source_url": source_url,
                "object_key": object_key,
                "ingest_type": ingest_type,
                "minio_site": minio_site,
                "mime_type": mime_type,
            },
        )
        return document_id

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, document_id: str) -> DocumentRow | None:
        row = await self._fetch_first(
            text("SELECT * FROM documents WHERE document_id = :id"),
            {"id": document_id},
        )
        return DocumentRow.from_mapping(row) if row else None

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    async def _claim(self, document_id: str, to_status: str, extra_set: str = "") -> DocumentRow:
        """Single-tx SELECT FOR UPDATE NOWAIT + UPDATE.

        Holds the row lock across both statements so a concurrent caller cannot
        observe the pre-transition status after we've decided to advance it (S28).
        """
        async with self._engine.begin() as conn:
            try:
                result = await conn.execute(
                    text("SELECT * FROM documents WHERE document_id = :id FOR UPDATE NOWAIT"),
                    {"id": document_id},
                )
            except OperationalError as exc:
                raise LockNotAvailable(document_id) from exc
            row = result.mappings().first()
            if row is None:
                raise LockNotAvailable(document_id)
            doc = DocumentRow.from_mapping(row)
            assert_transition(doc.status, to_status)
            await conn.execute(
                text(
                    f"UPDATE documents SET status=:to_status{extra_set},"
                    " updated_at=NOW(6) WHERE document_id=:id"
                ),
                {"id": document_id, "to_status": to_status},
            )
            return doc

    async def claim_for_processing(self, document_id: str) -> DocumentRow:
        return await self._claim(document_id, "PENDING", extra_set=", attempt=attempt+1")

    async def claim_for_deletion(self, document_id: str) -> DocumentRow:
        return await self._claim(document_id, "DELETING")

    # ------------------------------------------------------------------
    # Status mutations
    # ------------------------------------------------------------------

    async def update_status(
        self,
        document_id: str,
        from_status: str,
        to_status: str,
        attempt: int | None = None,
    ) -> None:
        assert_transition(from_status, to_status)
        params: dict = {"id": document_id, "from_status": from_status, "to_status": to_status}
        attempt_clause = ""
        if attempt is not None:
            attempt_clause = ", attempt = :attempt"
            params["attempt"] = attempt
        result = await self._execute(
            text(
                f"""
                UPDATE documents
                SET status = :to_status, updated_at = NOW(6){attempt_clause}
                WHERE document_id = :id AND status = :from_status
                """
            ),
            params,
        )
        if result.rowcount == 0:
            raise IllegalStateTransition(
                f"update_status: {from_status} → {to_status} failed for {document_id}"
            )

    async def update_heartbeat(self, document_id: str) -> None:
        await self._execute(
            text("UPDATE documents SET updated_at = NOW(6) WHERE document_id = :id"),
            {"id": document_id},
        )

    # ------------------------------------------------------------------
    # Stale queries (Reconciler)
    # ------------------------------------------------------------------

    async def list_pending_stale(
        self, updated_before: datetime.datetime, attempt_le: int
    ) -> list[DocumentRow]:
        rows = await self._fetch_all(
            text(
                """
                SELECT * FROM documents
                WHERE status = 'PENDING'
                  AND updated_at < :before
                  AND attempt <= :attempt_le
                """
            ),
            {"before": updated_before, "attempt_le": attempt_le},
        )
        return _rows_to_docs(rows)

    async def list_pending_exceeded(self, attempt_gt: int) -> list[DocumentRow]:
        rows = await self._fetch_all(
            text(
                """
                SELECT * FROM documents
                WHERE status = 'PENDING'
                  AND attempt > :attempt_gt
                """
            ),
            {"attempt_gt": attempt_gt},
        )
        return _rows_to_docs(rows)

    async def list_deleting_stale(self, updated_before: datetime.datetime) -> list[DocumentRow]:
        rows = await self._fetch_all(
            text(
                """
                SELECT * FROM documents
                WHERE status = 'DELETING'
                  AND updated_at < :before
                """
            ),
            {"before": updated_before},
        )
        return _rows_to_docs(rows)

    async def list_uploaded_stale(self, updated_before: datetime.datetime) -> list[DocumentRow]:
        rows = await self._fetch_all(
            text(
                """
                SELECT * FROM documents
                WHERE status = 'UPLOADED'
                  AND updated_at < :before
                """
            ),
            {"before": updated_before},
        )
        return _rows_to_docs(rows)

    # ------------------------------------------------------------------
    # List / pagination
    # ------------------------------------------------------------------

    async def list(self, after: str | None, limit: int) -> list[DocumentRow]:
        cursor_clause = " AND document_id > :after" if after else ""
        params: dict = {"limit": limit}
        if after:
            params["after"] = after
        rows = await self._fetch_all(
            text(
                f"SELECT * FROM documents WHERE 1=1{cursor_clause}"
                " ORDER BY document_id ASC LIMIT :limit"
            ),
            params,
        )
        return _rows_to_docs(rows)

    async def list_by_create_user(
        self, create_user: str, after: str | None, limit: int
    ) -> list[DocumentRow]:
        cursor_clause = " AND document_id > :after" if after else ""
        params: dict = {"user": create_user, "limit": limit}
        if after:
            params["after"] = after
        rows = await self._fetch_all(
            text(
                f"SELECT * FROM documents WHERE create_user = :user{cursor_clause}"
                " ORDER BY document_id ASC LIMIT :limit"
            ),
            params,
        )
        return _rows_to_docs(rows)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, document_id: str) -> None:
        await self._execute(
            text("DELETE FROM documents WHERE document_id = :id"),
            {"id": document_id},
        )

    # ------------------------------------------------------------------
    # Supersede helpers
    # ------------------------------------------------------------------

    async def list_ready_by_source(self, source_id: str, source_app: str) -> list[DocumentRow]:
        rows = await self._fetch_all(
            text(
                """
                SELECT * FROM documents
                WHERE source_id = :source_id AND source_app = :source_app AND status = 'READY'
                ORDER BY created_at ASC
                """
            ),
            {"source_id": source_id, "source_app": source_app},
        )
        return _rows_to_docs(rows)

    async def pop_oldest_loser_for_supersede(
        self, source_id: str, source_app: str, survivor_id: str
    ) -> DocumentRow | None:
        # Out-of-order finish safety: DB self-elects the survivor as the row
        # with MAX(created_at). The caller's survivor_id is honoured only when
        # it matches that elected row; otherwise this returns None, so a worker
        # that races finish order can never delete a strictly-newer survivor.
        row = await self._fetch_first(
            text(
                """
                SELECT d.* FROM documents d
                JOIN (
                    SELECT document_id FROM documents
                    WHERE source_id = :source_id
                      AND source_app = :source_app
                      AND status = 'READY'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) newest ON newest.document_id = :survivor_id
                WHERE d.source_id = :source_id
                  AND d.source_app = :source_app
                  AND d.status = 'READY'
                  AND d.document_id != :survivor_id
                ORDER BY d.created_at ASC
                LIMIT 1
                """
            ),
            {"source_id": source_id, "source_app": source_app, "survivor_id": survivor_id},
        )
        return DocumentRow.from_mapping(row) if row else None

    async def find_multi_ready_groups(self) -> list[tuple[str, str]]:
        rows = await self._fetch_all(
            text(
                """
                SELECT source_id, source_app FROM documents
                WHERE status = 'READY'
                GROUP BY source_id, source_app
                HAVING COUNT(*) > 1
                """
            )
        )
        return [(r["source_id"], r["source_app"]) for r in rows]

    # ------------------------------------------------------------------
    # Chat hydration
    # ------------------------------------------------------------------

    async def get_sources_by_document_ids(self, ids: list[str]) -> dict[str, tuple[str, str, str]]:
        if not ids:
            return {}
        # Hydration must surface only READY rows; mid-flight or DELETING docs
        # are not citable and would mismatch the ES chunks that retrieval saw.
        rows = await self._fetch_all(
            text(
                "SELECT document_id, source_app, source_id, source_title"
                " FROM documents WHERE document_id IN :ids AND status = 'READY'"
            ),
            {"ids": tuple(ids)},
        )
        return {
            r["document_id"]: (r["source_app"], r["source_id"], r["source_title"]) for r in rows
        }
