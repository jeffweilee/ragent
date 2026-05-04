"""T2.4 — ChunkRepository: bulk_insert / delete_by_document_id (spec §5.1)."""

from typing import Any

from sqlalchemy import text


class ChunkRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def bulk_insert(self, chunks: list[dict]) -> None:
        if not chunks:
            return
        self._conn.execute(
            text(
                """
                INSERT INTO chunks (chunk_id, document_id, ord, text, lang)
                VALUES (:chunk_id, :document_id, :ord, :text, :lang)
                """
            ),
            chunks,
        )

    def delete_by_document_id(self, document_id: str) -> None:
        self._conn.execute(
            text("DELETE FROM chunks WHERE document_id = :id"),
            {"id": document_id},
        )
