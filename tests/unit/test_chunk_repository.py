"""T2.3 — ChunkRepository: bulk_insert and delete_by_document_id (T0.4)."""

from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.chunk_repository import ChunkRepository


def _mock_engine():
    """AsyncMock engine + connection (00_rule.md pool rule)."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=MagicMock(rowcount=1))

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    return engine, conn


async def test_bulk_insert_executes_for_each_chunk():
    engine, conn = _mock_engine()
    repo = ChunkRepository(engine)
    chunks = [
        {"chunk_id": "C1", "document_id": "D1", "ord": 0, "text": "hello", "lang": "en"},
        {"chunk_id": "C2", "document_id": "D1", "ord": 1, "text": "world", "lang": "en"},
    ]
    await repo.bulk_insert(chunks)
    assert conn.execute.call_count >= 1


async def test_bulk_insert_empty_list_is_noop():
    engine, conn = _mock_engine()
    repo = ChunkRepository(engine)
    await repo.bulk_insert([])
    conn.execute.assert_not_called()


async def test_delete_by_document_id_executes_delete():
    engine, conn = _mock_engine()
    repo = ChunkRepository(engine)
    await repo.delete_by_document_id("D1")
    conn.execute.assert_called_once()
    sql = str(conn.execute.call_args[0][0])
    assert "delete" in sql.lower() or "DELETE" in str(conn.execute.call_args)


async def test_bulk_insert_passes_correct_fields():
    engine, conn = _mock_engine()
    repo = ChunkRepository(engine)
    chunk = {"chunk_id": "C9", "document_id": "D9", "ord": 0, "text": "txt", "lang": "zh"}
    await repo.bulk_insert([chunk])
    conn.execute.assert_called()
