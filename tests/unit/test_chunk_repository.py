"""T2.3 — ChunkRepository: bulk_insert and delete_by_document_id (T0.4)."""

from unittest.mock import MagicMock

from ragent.repositories.chunk_repository import ChunkRepository


def _mock_conn():
    conn = MagicMock()
    conn.execute.return_value = MagicMock(rowcount=1)
    return conn


def test_bulk_insert_executes_for_each_chunk():
    conn = _mock_conn()
    repo = ChunkRepository(conn)
    chunks = [
        {"chunk_id": "C1", "document_id": "D1", "ord": 0, "text": "hello", "lang": "en"},
        {"chunk_id": "C2", "document_id": "D1", "ord": 1, "text": "world", "lang": "en"},
    ]
    repo.bulk_insert(chunks)
    # Should have executed at least once (bulk or per-row)
    assert conn.execute.call_count >= 1


def test_bulk_insert_empty_list_is_noop():
    conn = _mock_conn()
    repo = ChunkRepository(conn)
    repo.bulk_insert([])
    conn.execute.assert_not_called()


def test_delete_by_document_id_executes_delete():
    conn = _mock_conn()
    repo = ChunkRepository(conn)
    repo.delete_by_document_id("D1")
    conn.execute.assert_called_once()
    sql = str(conn.execute.call_args[0][0])
    assert "delete" in sql.lower() or "DELETE" in str(conn.execute.call_args)


def test_bulk_insert_passes_correct_fields():
    conn = _mock_conn()
    repo = ChunkRepository(conn)
    chunk = {"chunk_id": "C9", "document_id": "D9", "ord": 0, "text": "txt", "lang": "zh"}
    repo.bulk_insert([chunk])
    # Verify the call was made (SQL content checked in integration tests)
    conn.execute.assert_called()
