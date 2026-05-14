"""T2.1 — DocumentRepository: all CRUD + locking methods (unit, mocked async connection)."""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.repositories.document_repository import (
    DocumentRepository,
    DocumentRow,
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
        source_meta=None,
        object_key="confluence_DOC-1_AAAA",
        status="UPLOADED",
        attempt=0,
        created_at=_dt("2026-01-01T00:00:00"),
        updated_at=_dt("2026-01-01T00:00:00"),
    )
    base.update(kwargs)
    return base


def _mock_engine(rows=None, rowcount=1):
    """Build an AsyncMock engine that doubles as connection for unit tests.

    repo uses `async with self._engine.begin() as conn: await conn.execute(...)`.
    """
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows or []
    result.mappings.return_value.first.return_value = rows[0] if rows else None
    result.rowcount = rowcount

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    return engine, conn


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_returns_document_id():
    engine, _ = _mock_engine()
    repo = DocumentRepository(engine)
    doc_id = await repo.create(
        document_id="DOCID001",
        create_user="alice",
        source_id="DOC-1",
        source_app="confluence",
        source_title="Title",
        object_key="confluence_DOC-1_DOCID001",
    )
    assert doc_id == "DOCID001"


async def test_create_inserts_all_mandatory_fields():
    engine, conn = _mock_engine()
    repo = DocumentRepository(engine)
    await repo.create(
        document_id="ID1",
        create_user="bob",
        source_id="S1",
        source_app="slack",
        source_title="A Title",
        object_key="slack_S1_ID1",
    )
    conn.execute.assert_called_once()


async def test_create_with_optional_source_meta():
    engine, _ = _mock_engine()
    repo = DocumentRepository(engine)
    doc_id = await repo.create(
        document_id="ID2",
        create_user="carol",
        source_id="S2",
        source_app="jira",
        source_title="T",
        object_key="jira_S2_ID2",
        source_meta="eng",
    )
    assert doc_id == "ID2"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_document_row():
    row = _row(document_id="ID1", status="READY")
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    doc = await repo.get("ID1")
    assert doc is not None
    assert isinstance(doc, DocumentRow)
    assert doc.document_id == "ID1"
    assert doc.status == "READY"
    assert doc.source_title == "My Title"
    assert doc.source_app == "confluence"
    assert doc.source_meta is None


async def test_get_returns_none_for_missing():
    engine, _ = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    assert await repo.get("MISSING") is None


async def test_get_returns_all_fields():
    row = _row(
        document_id="ID3",
        create_user="dave",
        source_id="DOC-3",
        source_app="notion",
        source_title="My Doc",
        source_meta="hr",
        object_key="notion_DOC-3_ID3",
        status="PENDING",
        attempt=2,
        created_at=_dt("2026-01-02T10:00:00"),
        updated_at=_dt("2026-01-02T10:05:00"),
    )
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    doc = await repo.get("ID3")
    assert doc.create_user == "dave"
    assert doc.source_meta == "hr"
    assert doc.attempt == 2


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


async def test_update_status_valid_transition():
    engine, conn = _mock_engine(rowcount=1)
    repo = DocumentRepository(engine)
    await repo.update_status("ID1", from_status="UPLOADED", to_status="PENDING", attempt=1)
    conn.execute.assert_called()


async def test_update_status_invalid_transition_raises():
    engine, _ = _mock_engine(rowcount=0)
    repo = DocumentRepository(engine)
    from ragent.utility.state_machine import IllegalStateTransition

    with pytest.raises(IllegalStateTransition):
        await repo.update_status("ID1", from_status="READY", to_status="PENDING")


# ---------------------------------------------------------------------------
# update_heartbeat
# ---------------------------------------------------------------------------


async def test_update_heartbeat_executes_update():
    engine, conn = _mock_engine()
    repo = DocumentRepository(engine)
    await repo.update_heartbeat("ID1")
    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# list_pending_stale
# ---------------------------------------------------------------------------


async def test_list_pending_stale_returns_rows():
    row = _row(status="PENDING", attempt=1)
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    stale_before = _dt("2026-01-01T00:04:00")
    results = await repo.list_pending_stale(updated_before=stale_before, attempt_le=5)
    assert len(results) == 1
    assert results[0].status == "PENDING"


# ---------------------------------------------------------------------------
# list_uploaded_stale
# ---------------------------------------------------------------------------


async def test_list_uploaded_stale_returns_rows():
    row = _row(status="UPLOADED", attempt=0)
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    stale_before = _dt("2026-01-01T00:04:00")
    results = await repo.list_uploaded_stale(updated_before=stale_before)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_rows_with_cursor():
    rows = [_row(document_id=f"ID{i}") for i in range(3)]
    engine, _ = _mock_engine(rows=rows)
    repo = DocumentRepository(engine)
    results = await repo.list(after=None, limit=10)
    assert len(results) == 3


async def test_list_after_cursor_filters():
    row = _row(document_id="ID5")
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    results = await repo.list(after="ID4", limit=5)
    assert results[0].document_id == "ID5"


async def test_list_uses_desc_order_and_lt_cursor():
    """Cursor for DESC ordering must use < (not >) and SQL must be DESC."""
    engine, conn = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    await repo.list(after="ID4", limit=5)
    call_args = conn.execute.call_args
    sql_text = str(call_args[0][0])
    assert "DESC" in sql_text.upper(), "expected ORDER BY … DESC"
    assert "< :after" in sql_text or "< :cursor" in sql_text, "expected < cursor condition for DESC"


async def test_list_source_id_filter_adds_where_clause():
    engine, conn = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    await repo.list(after=None, limit=10, source_id="DOC-1")
    sql_text = str(conn.execute.call_args[0][0])
    assert "source_id" in sql_text


async def test_list_source_app_filter_adds_where_clause():
    engine, conn = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    await repo.list(after=None, limit=10, source_app="confluence")
    sql_text = str(conn.execute.call_args[0][0])
    assert "source_app" in sql_text


async def test_list_source_id_and_source_app_combined():
    engine, conn = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    await repo.list(after=None, limit=10, source_id="DOC-1", source_app="confluence")
    sql_text = str(conn.execute.call_args[0][0])
    assert "source_id" in sql_text
    assert "source_app" in sql_text


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_executes_delete_sql():
    engine, conn = _mock_engine(rowcount=1)
    repo = DocumentRepository(engine)
    await repo.delete("ID1")
    conn.execute.assert_called()


# ---------------------------------------------------------------------------
# claim_for_processing — atomic conditional UPDATE + post-state SELECT
# ---------------------------------------------------------------------------


async def test_claim_for_processing_returns_document_row_when_claimed():
    row = _row(status="PENDING", attempt=1)
    engine, _ = _mock_engine(rows=[row], rowcount=1)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_processing("ID1")
    assert doc is not None
    assert doc.status == "PENDING"
    assert doc.document_id == "AAAAAAAAAAAAAAAAAAAAAAAAAAA"


async def test_claim_for_processing_returns_none_when_status_terminal():
    """READY/FAILED/DELETING source state → not in accept_from → None."""
    row = _row(status="READY")
    engine, _ = _mock_engine(rows=[row], rowcount=1)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_processing("ID1")
    assert doc is None


async def test_claim_for_processing_returns_none_when_row_missing():
    engine, _ = _mock_engine(rows=[], rowcount=0)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_processing("MISSING")
    assert doc is None


async def test_claim_for_processing_returns_prior_status():
    """Returned DocumentRow carries the *prior* status, so callers can branch
    on what the row was before the transition."""
    row = _row(status="UPLOADED", attempt=0)
    engine, _ = _mock_engine(rows=[row], rowcount=1)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_processing("ID1")
    assert doc is not None
    assert doc.status == "UPLOADED"  # pre-state, not "PENDING"
    assert doc.attempt == 0  # pre-bump


async def test_claim_for_processing_returns_none_on_toctou_race():
    """SELECT shows status in accept_from, but UPDATE rowcount=0: another writer
    raced us between the SELECT and UPDATE statements."""
    row = _row(status="UPLOADED")
    engine, _ = _mock_engine(rows=[row], rowcount=0)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_processing("ID1")
    assert doc is None


# ---------------------------------------------------------------------------
# claim_for_deletion — atomic conditional UPDATE + post-state SELECT
# ---------------------------------------------------------------------------


async def test_claim_for_deletion_returns_document_row_when_claimed():
    row = _row(status="READY")
    engine, _ = _mock_engine(rows=[row], rowcount=1)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_deletion("ID1")
    assert doc is not None
    assert doc.status == "READY"  # pre-state


async def test_claim_for_deletion_returns_none_when_already_deleting():
    row = _row(status="DELETING")
    engine, _ = _mock_engine(rows=[row], rowcount=1)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_deletion("ID1")
    assert doc is None  # DELETING not in accept_from


async def test_claim_for_deletion_returns_none_when_row_missing():
    engine, _ = _mock_engine(rows=[], rowcount=0)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_deletion("MISSING")
    assert doc is None


async def test_claim_for_deletion_returns_prior_status_for_cascade_branching():
    """Cascade in IngestService.delete reads doc.status to decide MinIO
    cleanup; the prior status must survive the claim."""
    row = _row(status="UPLOADED")
    engine, _ = _mock_engine(rows=[row], rowcount=1)
    repo = DocumentRepository(engine)
    doc = await repo.claim_for_deletion("ID1")
    assert doc is not None
    assert doc.status == "UPLOADED"  # pre-state, drives MinIO delete branch


# ---------------------------------------------------------------------------
# list_ready_by_source
# ---------------------------------------------------------------------------


async def test_list_ready_by_source_returns_rows():
    row = _row(status="READY")
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    results = await repo.list_ready_by_source(source_id="DOC-1", source_app="confluence")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# pop_oldest_loser_for_supersede
# ---------------------------------------------------------------------------


async def test_pop_oldest_loser_returns_row_or_none():
    row = _row(status="READY", document_id="OLD-ID")
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    result = await repo.pop_oldest_loser_for_supersede(
        source_id="DOC-1", source_app="confluence", survivor_id="NEW-ID"
    )
    assert result is None or result.document_id == "OLD-ID"


async def test_pop_oldest_loser_returns_none_when_no_loser():
    engine, _ = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    result = await repo.pop_oldest_loser_for_supersede(
        source_id="DOC-1", source_app="confluence", survivor_id="NEW-ID"
    )
    assert result is None


async def test_pop_oldest_loser_query_elects_survivor_via_db_max_created_at():
    """SQL must self-elect the survivor by MAX(created_at), not trust the
    caller's survivor_id, so a worker that races finish-order can never
    delete a strictly-newer row by mistake (out-of-order safety)."""
    engine, conn = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    await repo.pop_oldest_loser_for_supersede(
        source_id="DOC-1", source_app="confluence", survivor_id="MAYBE-NOT-NEWEST"
    )
    assert conn.execute.await_count == 1
    sql = str(conn.execute.await_args.args[0])
    # Inner subquery elects newest by created_at DESC LIMIT 1.
    assert "ORDER BY created_at DESC" in sql
    # JOIN couples loser-pop to that elected newest = caller's survivor_id.
    assert "JOIN" in sql.upper()
    assert "newest.document_id = :survivor_id" in sql


# ---------------------------------------------------------------------------
# find_multi_ready_groups
# ---------------------------------------------------------------------------


async def test_find_multi_ready_groups_returns_pairs():
    engine, _ = _mock_engine(rows=[{"source_id": "DOC-1", "source_app": "confluence"}])
    repo = DocumentRepository(engine)
    groups = await repo.find_multi_ready_groups()
    assert groups == [("DOC-1", "confluence")]


# ---------------------------------------------------------------------------
# get_sources_by_document_ids
# ---------------------------------------------------------------------------


async def test_get_sources_by_document_ids_returns_map():
    rows = [
        {"document_id": "ID1", "source_app": "confluence", "source_id": "S1", "source_title": "T1"},
        {"document_id": "ID2", "source_app": "slack", "source_id": "S2", "source_title": "T2"},
    ]
    engine, _ = _mock_engine(rows=rows)
    repo = DocumentRepository(engine)
    result = await repo.get_sources_by_document_ids(["ID1", "ID2"])
    assert result["ID1"] == ("confluence", "S1", "T1")
    assert result["ID2"] == ("slack", "S2", "T2")


async def test_get_sources_by_document_ids_empty_input_returns_empty():
    engine, _ = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    result = await repo.get_sources_by_document_ids([])
    assert result == {}


async def test_get_sources_by_document_ids_filters_to_status_ready():
    """Hydration must only return READY rows; mid-flight or DELETING docs
    are not citable and would mismatch the ES chunks retrieval saw."""
    engine, conn = _mock_engine(rows=[])
    repo = DocumentRepository(engine)
    await repo.get_sources_by_document_ids(["ID1"])
    assert conn.execute.await_count == 1
    sql = str(conn.execute.await_args.args[0])
    assert "status = 'READY'" in sql


# ---------------------------------------------------------------------------
# list_by_create_user
# ---------------------------------------------------------------------------


async def test_list_by_create_user_returns_rows():
    row = _row(create_user="alice")
    engine, _ = _mock_engine(rows=[row])
    repo = DocumentRepository(engine)
    results = await repo.list_by_create_user(create_user="alice", after=None, limit=10)
    assert len(results) == 1
    assert results[0].create_user == "alice"
