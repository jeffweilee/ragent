"""T2.4 — ChunkRepository: bulk_insert / delete_by_document_id (spec §5.1).

Per `docs/00_rule.md` Database Practices: every method checks out a fresh
connection from the engine's pool. No long-lived shared connection.
"""

from typing import Any

from sqlalchemy import text


class ChunkRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def bulk_insert(self, chunks: list[dict]) -> None:
        if not chunks:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO chunks (chunk_id, document_id, ord, text, lang)
                    VALUES (:chunk_id, :document_id, :ord, :text, :lang)
                    """
                ),
                chunks,
            )

    def delete_by_document_id(self, document_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM chunks WHERE document_id = :id"),
                {"id": document_id},
            )
