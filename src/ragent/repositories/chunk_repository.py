"""T2.4 / TA.2 — ChunkRepository: async bulk_insert / delete_by_document_id (spec §5.1).

Per `docs/00_rule.md` Database Practices: every method checks out a fresh
async connection from the engine's pool. All methods are `async def`.
"""

from typing import Any

from sqlalchemy import text


class ChunkRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def bulk_insert(self, chunks: list[dict]) -> None:
        if not chunks:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO chunks (chunk_id, document_id, ord, text, lang)
                    VALUES (:chunk_id, :document_id, :ord, :text, :lang)
                    """
                ),
                chunks,
            )

    async def delete_by_document_id(self, document_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM chunks WHERE document_id = :id"),
                {"id": document_id},
            )
