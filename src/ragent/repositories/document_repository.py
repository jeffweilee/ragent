"""T2.2 / TA.2 — DocumentRepository: async CRUD + locking (spec §5.1, B11, B14, B17).

Per `docs/00_rule.md` Database Practices: every method checks out a fresh
async connection from the engine's pool and releases it on exit. All methods
are `async def` for direct use in FastAPI routes and TaskIQ tasks.

Sync bridge for Haystack pipeline components (anyio threads): use
`anyio.from_thread.run(repo.method, *args)`.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.exc import OperationalError

from ragent.utility.state_machine import IllegalStateTransition, assert_transition

logger = structlog.get_logger(__name__)

_MARIADB_DEADLOCK = 1213


def _is_deadlock(exc: OperationalError) -> bool:
    return getattr(getattr(exc, "orig", None), "args", (None,))[0] == _MARIADB_DEADLOCK


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
    # 006_documents_error_code.sql: persisted failure diagnostics.
    error_code: str | None = None
    error_reason: str | None = None

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
            error_code=m.get("error_code"),
            error_reason=m.get("error_reason"),
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

    def _log_transition(
        self, document_id: str, from_status: str, to_status: str, **extra: Any
    ) -> None:
        logger.info(
            "documents.status.transition",
            document_id=document_id,
            from_status=from_status,
            to_status=to_status,
            **extra,
        )

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

    async def _atomic_claim(
        self,
        document_id: str,
        to_status: str,
        accept_from: tuple[str, ...],
        bump_attempt: bool = False,
    ) -> DocumentRow | None:
        """Pre-state SELECT + atomic conditional UPDATE.

        Reads the row, then runs ``UPDATE … WHERE status IN (:accept_from)``.
        Returns the **pre-state** ``DocumentRow`` on a successful transition
        so callers can branch on the prior status (e.g. delete-cascade
        chooses MinIO cleanup based on whether the doc was UPLOADED/PENDING).
        Returns ``None`` when the row is missing, the prior status isn't in
        ``accept_from``, or a concurrent writer transitioned the row out of
        the accept-set between our SELECT and UPDATE (rowcount=0). InnoDB's
        per-statement row lock on the UPDATE serialises concurrent claimers.
        """
        extra_set = ", attempt=attempt+1" if bump_attempt else ""
        update_stmt = text(
            f"UPDATE documents SET status=:to_status{extra_set},"
            " updated_at=NOW(6) WHERE document_id=:id"
            " AND status IN :accept_from"
        ).bindparams(bindparam("accept_from", expanding=True))
        async with self._engine.begin() as conn:
            pre = (
                (
                    await conn.execute(
                        text("SELECT * FROM documents WHERE document_id = :id"),
                        {"id": document_id},
                    )
                )
                .mappings()
                .first()
            )
            if pre is None or pre["status"] not in accept_from:
                return None
            update_result = await conn.execute(
                update_stmt,
                {
                    "id": document_id,
                    "to_status": to_status,
                    "accept_from": list(accept_from),
                },
            )
            if update_result.rowcount == 0:
                return None
            self._log_transition(document_id, pre["status"], to_status)
            return DocumentRow.from_mapping(pre)

    async def claim_for_processing(self, document_id: str) -> DocumentRow | None:
        """Claim the row for ingest processing.

        Accepts UPLOADED (organic POST→worker hand-off) or PENDING (reconciler
        redispatch / manual rerun). Returns the pre-state row (``status`` is
        the prior value, ``attempt`` is pre-bump) or ``None`` if the row is
        terminal (READY/FAILED/DELETING) or missing.
        """
        return await self._atomic_claim(
            document_id,
            to_status="PENDING",
            accept_from=("UPLOADED", "PENDING"),
            bump_attempt=True,
        )

    async def claim_for_deletion(self, document_id: str) -> DocumentRow | None:
        """Claim the row for deletion.

        Returns the pre-state row (``status`` reflects the prior value so the
        cascade can branch on UPLOADED/PENDING for MinIO cleanup) or ``None``
        if already DELETING or missing.
        """
        return await self._atomic_claim(
            document_id,
            to_status="DELETING",
            accept_from=("UPLOADED", "PENDING", "READY", "FAILED"),
        )

    async def mark_for_rerun(
        self, document_id: str
    ) -> Literal["ok", "not_rerunnable", "not_found"]:
        """Transition any non-READY/non-DELETING row back to PENDING for manual rerun.

        Returns ``"ok"`` on success, ``"not_rerunnable"`` if the row exists
        but is in a terminal/in-flight-delete state (READY or DELETING), and
        ``"not_found"`` if no row matches ``document_id``. Clears
        ``error_code``/``error_reason`` and refreshes ``updated_at`` so the
        reconciler's stale-sweep doesn't immediately race the manual dispatch.
        Resets ``attempt`` to 0 so an exhausted FAILED row (the primary use
        case for manual rerun) isn't immediately swept back to FAILED by the
        reconciler's ``_mark_failed`` budget check before the worker picks up
        — the operator's intent on /rerun is "start a fresh attempt budget".
        """
        async with self._engine.begin() as conn:
            update_result = await conn.execute(
                text(
                    "UPDATE documents SET status='PENDING', attempt=0,"
                    " updated_at=NOW(6), error_code=NULL, error_reason=NULL"
                    " WHERE document_id=:id AND status IN ('UPLOADED','PENDING','FAILED')"
                ),
                {"id": document_id},
            )
            if update_result.rowcount == 1:
                self._log_transition(document_id, "<rerun>", "PENDING")
                return "ok"
            exists = (
                (
                    await conn.execute(
                        text("SELECT 1 FROM documents WHERE document_id=:id"),
                        {"id": document_id},
                    )
                )
                .mappings()
                .first()
            )
            return "not_rerunnable" if exists else "not_found"

    # ------------------------------------------------------------------
    # Status mutations
    # ------------------------------------------------------------------

    async def update_status(
        self,
        document_id: str,
        from_status: str,
        to_status: str,
        attempt: int | None = None,
        error_code: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        assert_transition(from_status, to_status)
        # MariaDB VARCHAR(255) — truncate proactively rather than surface a
        # "Data too long" surprise on long stack messages at the failure path.
        params: dict = {
            "id": document_id,
            "from_status": from_status,
            "to_status": to_status,
            "error_code": error_code,
            "error_reason": error_reason[:255] if error_reason else None,
        }
        attempt_clause = ""
        if attempt is not None:
            attempt_clause = ", attempt = :attempt"
            params["attempt"] = attempt
        result = await self._execute(
            text(
                f"""
                UPDATE documents
                SET status = :to_status, updated_at = NOW(6){attempt_clause},
                    error_code = :error_code, error_reason = :error_reason
                WHERE document_id = :id AND status = :from_status
                """
            ),
            params,
        )
        if result.rowcount == 0:
            raise IllegalStateTransition(
                f"update_status: {from_status} → {to_status} failed for {document_id}"
            )
        self._log_transition(document_id, from_status, to_status, error_code=error_code)

    async def update_heartbeat(self, document_id: str) -> None:
        await self._execute(
            text("UPDATE documents SET updated_at = NOW(6) WHERE document_id = :id"),
            {"id": document_id},
        )

    async def promote_to_ready_and_demote_siblings(
        self, document_id: str, source_id: str, source_app: str
    ) -> bool:
        """Atomic, DB-arbitrated READY transition for (source_id, source_app).

        Returns ``True`` if the caller is the survivor (promoted to READY),
        ``False`` if the caller self-demoted (a newer sibling exists or the
        row is no longer PENDING). Callers should gate post-READY side effects
        on this return value.

        Retries up to 3 times on MariaDB deadlock (error 1213) which can
        occur when concurrent tasks for the same source_id group race on
        the row-level locks in the election subquery.
        """
        assert_transition("PENDING", "READY")
        for attempt in range(3):
            try:
                return await self._promote_or_demote(document_id, source_id, source_app)
            except OperationalError as exc:
                if attempt < 2 and _is_deadlock(exc):
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                raise

    async def _promote_or_demote(self, document_id: str, source_id: str, source_app: str) -> bool:
        async with self._engine.begin() as conn:
            # Derived table required: MariaDB forbids referencing the UPDATE
            # target table directly in the subquery predicate.
            promoted = await conn.execute(
                text(
                    """
                    UPDATE documents
                    SET status = 'READY', updated_at = NOW(6)
                    WHERE document_id = :id
                      AND status = 'PENDING'
                      AND document_id = (
                        SELECT document_id FROM (
                          SELECT document_id FROM documents
                          WHERE source_id = :src
                            AND source_app = :app
                            AND status IN ('PENDING', 'READY')
                          ORDER BY created_at DESC, document_id DESC
                          LIMIT 1
                        ) AS elected
                      )
                    """
                ),
                {"id": document_id, "src": source_id, "app": source_app},
            )

            if promoted.rowcount == 1:
                await conn.execute(
                    text(
                        """
                        UPDATE documents
                        SET status = 'DELETING', updated_at = NOW(6)
                        WHERE source_id = :src
                          AND source_app = :app
                          AND document_id != :id
                          AND status = 'READY'
                        """
                    ),
                    {"src": source_id, "app": source_app, "id": document_id},
                )
                self._log_transition(document_id, "PENDING", "READY")
                return True

            demoted = await conn.execute(
                text(
                    """
                    UPDATE documents
                    SET status = 'DELETING', updated_at = NOW(6)
                    WHERE document_id = :id AND status = 'PENDING'
                    """
                ),
                {"id": document_id},
            )
            if demoted.rowcount == 1:
                self._log_transition(document_id, "PENDING", "DELETING")
            return False

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

    async def list(
        self,
        after: str | None,
        limit: int,
        source_id: str | None = None,
        source_app: str | None = None,
    ) -> list[DocumentRow]:
        conditions: list[str] = []
        params: dict = {"limit": limit}
        if after:
            conditions.append("document_id < :after")
            params["after"] = after
        if source_id is not None:
            conditions.append("source_id = :source_id")
            params["source_id"] = source_id
        if source_app is not None:
            conditions.append("source_app = :source_app")
            params["source_app"] = source_app
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = await self._fetch_all(
            text(f"SELECT * FROM documents {where} ORDER BY document_id DESC LIMIT :limit"),
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
                    ORDER BY created_at DESC, document_id DESC
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

    async def get_document_ids_by_source(
        self, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        """Map (source_app, source_id) → current READY document_id (T-FB.7).

        Used by the feedback retriever to translate (source_app, source_id)
        from `feedback_v1` (its B50 key) into the document_id key needed
        to fetch chunks from `chunks_v1`. Returns the most recent READY row
        per pair; pairs with no READY row are absent from the result.
        """
        if not pairs:
            return {}
        rows = await self._fetch_all(
            text(
                "SELECT source_app, source_id, document_id, created_at"
                " FROM documents"
                " WHERE (source_app, source_id) IN :pairs AND status = 'READY'"
                " ORDER BY created_at DESC"
            ).bindparams(bindparam("pairs", expanding=True)),
            {"pairs": [tuple(p) for p in pairs]},
        )
        # Multiple READY rows for the same pair would violate B41, but be
        # tolerant: keep the newest (rows already sorted DESC).
        out: dict[tuple[str, str], str] = {}
        for r in rows:
            key = (r["source_app"], r["source_id"])
            if key not in out:
                out[key] = r["document_id"]
        return out
