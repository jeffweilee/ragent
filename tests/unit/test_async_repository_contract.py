"""TA.1 — Async repository contract: all public methods must be coroutine functions.

Verifies the aiomysql adoption decision: DocumentRepository and ChunkRepository
expose only async methods so FastAPI/TaskIQ callers can await them directly.
"""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.repositories.chunk_repository import ChunkRepository
from ragent.repositories.document_repository import (
    DocumentRepository,
    DocumentRow,
    LockNotAvailable,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.UTC)


def _row(**kwargs) -> dict:
    base = dict(
        document_id="AAAAAAAAAAAAAAAAAAAAAAAAAAA",
        create_user="alice",
        source_id="DOC-1",
        source_app="confluence",
        source_title="My Title",
        source_workspace=None,
        object_key="confluence_DOC-1_AAAA",
        status="UPLOADED",
        attempt=0,
        created_at=_dt("2026-01-01T00:00:00"),
        updated_at=_dt("2026-01-01T00:00:00"),
    )
    base.update(kwargs)
    return base


def _make_async_engine(rows=None, rowcount=1):
    """Build an AsyncMock engine that behaves like SQLAlchemy AsyncEngine."""
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows or []
    result.mappings.return_value.first.return_value = rows[0] if rows else None
    result.rowcount = rowcount

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)

    # async with engine.begin() as conn:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    # also support engine.connect() for health probe
    engine.connect = MagicMock(return_value=ctx)
    return engine, conn


# ---------------------------------------------------------------------------
# Contract: all DocumentRepository methods are coroutine functions
# ---------------------------------------------------------------------------

DOC_REPO_ASYNC_METHODS = [
    "create",
    "get",
    "acquire_nowait",
    "update_status",
    "update_heartbeat",
    "list_pending_stale",
    "list_pending_exceeded",
    "list_deleting_stale",
    "list_uploaded_stale",
    "list",
    "list_by_create_user",
    "delete",
    "list_ready_by_source",
    "pop_oldest_loser_for_supersede",
    "find_multi_ready_groups",
    "get_sources_by_document_ids",
]


@pytest.mark.parametrize("method_name", DOC_REPO_ASYNC_METHODS)
def test_document_repository_method_is_coroutine(method_name: str) -> None:
    engine, _ = _make_async_engine()
    repo = DocumentRepository(engine)
    method = getattr(repo, method_name)
    assert asyncio.iscoroutinefunction(method), (
        f"DocumentRepository.{method_name} must be an async def"
    )


CHUNK_REPO_ASYNC_METHODS = ["bulk_insert", "delete_by_document_id"]


@pytest.mark.parametrize("method_name", CHUNK_REPO_ASYNC_METHODS)
def test_chunk_repository_method_is_coroutine(method_name: str) -> None:
    engine, _ = _make_async_engine()
    repo = ChunkRepository(engine)
    method = getattr(repo, method_name)
    assert asyncio.iscoroutinefunction(method), (
        f"ChunkRepository.{method_name} must be an async def"
    )


# ---------------------------------------------------------------------------
# DocumentRepository — async CRUD behaviour
# ---------------------------------------------------------------------------


async def test_async_create_returns_document_id() -> None:
    engine, _ = _make_async_engine()
    repo = DocumentRepository(engine)
    doc_id = await repo.create(
        document_id="DOC001",
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="Title",
        object_key="confluence_S1_DOC001",
    )
    assert doc_id == "DOC001"


async def test_async_get_returns_document_row() -> None:
    row = _row(document_id="ID1", status="READY")
    engine, conn = _make_async_engine(rows=[row])
    repo = DocumentRepository(engine)
    doc = await repo.get("ID1")
    assert doc is not None
    assert isinstance(doc, DocumentRow)
    assert doc.status == "READY"


async def test_async_get_returns_none_for_missing() -> None:
    engine, _ = _make_async_engine(rows=[])
    repo = DocumentRepository(engine)
    assert await repo.get("MISSING") is None


async def test_async_acquire_nowait_raises_lock_not_available_on_operational_error() -> None:
    from sqlalchemy.exc import OperationalError

    engine, conn = _make_async_engine()
    conn.execute.side_effect = OperationalError("s", {}, Exception("lock"))
    repo = DocumentRepository(engine)
    with pytest.raises(LockNotAvailable):
        await repo.acquire_nowait("ID1")


async def test_async_update_status_valid_transition() -> None:
    engine, conn = _make_async_engine(rowcount=1)
    repo = DocumentRepository(engine)
    await repo.update_status("ID1", from_status="UPLOADED", to_status="PENDING", attempt=1)
    conn.execute.assert_called()


async def test_async_update_status_invalid_transition_raises() -> None:
    from ragent.utility.state_machine import IllegalStateTransition

    engine, _ = _make_async_engine(rowcount=0)
    repo = DocumentRepository(engine)
    with pytest.raises(IllegalStateTransition):
        await repo.update_status("ID1", from_status="READY", to_status="PENDING")


async def test_async_update_heartbeat_executes() -> None:
    engine, conn = _make_async_engine()
    repo = DocumentRepository(engine)
    await repo.update_heartbeat("ID1")
    conn.execute.assert_called_once()


async def test_async_list_returns_rows() -> None:
    rows = [_row(document_id=f"ID{i}") for i in range(3)]
    engine, _ = _make_async_engine(rows=rows)
    repo = DocumentRepository(engine)
    results = await repo.list(after=None, limit=10)
    assert len(results) == 3


async def test_async_delete_executes() -> None:
    engine, conn = _make_async_engine()
    repo = DocumentRepository(engine)
    await repo.delete("ID1")
    conn.execute.assert_called()


async def test_async_get_sources_by_document_ids_returns_map() -> None:
    rows = [
        {"document_id": "ID1", "source_app": "confluence", "source_id": "S1", "source_title": "T1"},
    ]
    engine, _ = _make_async_engine(rows=rows)
    repo = DocumentRepository(engine)
    result = await repo.get_sources_by_document_ids(["ID1"])
    assert result["ID1"] == ("confluence", "S1", "T1")


async def test_async_get_sources_by_document_ids_empty_returns_empty() -> None:
    engine, _ = _make_async_engine()
    repo = DocumentRepository(engine)
    assert await repo.get_sources_by_document_ids([]) == {}


# ---------------------------------------------------------------------------
# ChunkRepository — async behaviour
# ---------------------------------------------------------------------------


async def test_async_bulk_insert_executes() -> None:
    engine, conn = _make_async_engine()
    repo = ChunkRepository(engine)
    chunks = [{"chunk_id": "C1", "document_id": "D1", "ord": 0, "text": "hi", "lang": "en"}]
    await repo.bulk_insert(chunks)
    conn.execute.assert_called_once()


async def test_async_bulk_insert_empty_is_noop() -> None:
    engine, conn = _make_async_engine()
    repo = ChunkRepository(engine)
    await repo.bulk_insert([])
    conn.execute.assert_not_called()


async def test_async_delete_by_document_id_executes() -> None:
    engine, conn = _make_async_engine()
    repo = ChunkRepository(engine)
    await repo.delete_by_document_id("D1")
    conn.execute.assert_called_once()
