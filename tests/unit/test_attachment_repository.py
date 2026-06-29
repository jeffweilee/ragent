"""T-CAT.8 — AttachmentRepository: CRUD + list_by_thread, update_status (unit, mocked conn)."""

import datetime
from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.attachment_repository import (
    ArtifactRow,
    AttachmentRepository,
    AttachmentRow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)


def _row(**kwargs) -> dict:
    base = dict(
        attachment_id="ATTAAAAAAAAAAAAAAAAAAAAAA",
        thread_id="thread-1",
        create_user="alice",
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=12345,
        status="UPLOADED",
        created_at=_dt("2026-01-01T00:00:00"),
        updated_at=_dt("2026-01-01T00:00:00"),
    )
    base.update(kwargs)
    return base


def _artifact_row(**kwargs) -> dict:
    base = dict(
        attachment_id="ATTAAAAAAAAAAAAAAAAAAAAAA",
        variant="complete",
        storage_key="chat_attachments/ATT.../complete.json",
        content_type="text/markdown",
        char_count=42,
        created_at=_dt("2026-01-01T00:00:00"),
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


def _mock_claim_engine(pre_row, update_rowcount):
    """Engine whose conn.execute() returns the SELECT result, then the UPDATE result.

    Mirrors `claim_for_processing`'s two-statement transaction: a pre-state
    SELECT followed by a conditional UPDATE.
    """
    pre_result = MagicMock()
    pre_result.mappings.return_value.first.return_value = pre_row

    update_result = MagicMock()
    update_result.rowcount = update_rowcount

    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=[pre_result, update_result])

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    return engine, conn


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_returns_attachment_id():
    engine, _ = _mock_engine()
    repo = AttachmentRepository(engine)
    attachment_id = await repo.create(
        attachment_id="ATT001",
        thread_id="thread-1",
        create_user="alice",
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=12345,
    )
    assert attachment_id == "ATT001"


async def test_create_inserts_all_mandatory_fields():
    engine, conn = _mock_engine()
    repo = AttachmentRepository(engine)
    await repo.create(
        attachment_id="ATT001",
        thread_id="thread-1",
        create_user="alice",
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=12345,
    )
    conn.execute.assert_called_once()
    params = conn.execute.call_args[0][1]
    assert params["attachment_id"] == "ATT001"
    assert params["thread_id"] == "thread-1"
    assert params["create_user"] == "alice"
    assert params["filename"] == "report.pdf"
    assert params["mime_type"] == "application/pdf"
    assert params["size_bytes"] == 12345


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_attachment_row():
    row = _row(attachment_id="ATT001", status="READY")
    engine, _ = _mock_engine(rows=[row])
    repo = AttachmentRepository(engine)
    attachment = await repo.get("ATT001")
    assert attachment is not None
    assert isinstance(attachment, AttachmentRow)
    assert attachment.attachment_id == "ATT001"
    assert attachment.status == "READY"
    assert attachment.thread_id == "thread-1"
    assert attachment.filename == "report.pdf"


async def test_get_returns_none_for_missing():
    engine, _ = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    assert await repo.get("MISSING") is None


async def test_get_filters_by_create_user_when_provided():
    engine, conn = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    await repo.get("ATT001", create_user="alice")
    sql_text = str(conn.execute.call_args[0][0])
    params = conn.execute.call_args[0][1]
    assert "create_user" in sql_text
    assert params["create_user"] == "alice"


async def test_get_omits_create_user_filter_when_not_provided():
    engine, conn = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    await repo.get("ATT001")
    sql_text = str(conn.execute.call_args[0][0])
    assert "create_user" not in sql_text


# ---------------------------------------------------------------------------
# list_by_thread
# ---------------------------------------------------------------------------


async def test_list_by_thread_returns_rows():
    rows = [_row(attachment_id=f"ATT{i}") for i in range(3)]
    engine, _ = _mock_engine(rows=rows)
    repo = AttachmentRepository(engine)
    results = await repo.list_by_thread("thread-1")
    assert len(results) == 3
    assert all(isinstance(r, AttachmentRow) for r in results)


async def test_list_by_thread_filters_by_thread_id():
    engine, conn = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    await repo.list_by_thread("thread-1")
    sql_text = str(conn.execute.call_args[0][0])
    assert "thread_id" in sql_text


async def test_list_by_thread_after_cursor_filters():
    row = _row(attachment_id="ATT5")
    engine, conn = _mock_engine(rows=[row])
    repo = AttachmentRepository(engine)
    results = await repo.list_by_thread("thread-1", after="ATT9", limit=5)
    assert results[0].attachment_id == "ATT5"
    sql_text = str(conn.execute.call_args[0][0])
    assert "< :after" in sql_text


async def test_list_by_thread_filters_by_create_user_when_provided():
    engine, conn = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    await repo.list_by_thread("thread-1", create_user="alice")
    sql_text = str(conn.execute.call_args[0][0])
    params = conn.execute.call_args[0][1]
    assert "create_user" in sql_text
    assert params["create_user"] == "alice"


async def test_list_by_thread_omits_create_user_filter_when_not_provided():
    engine, conn = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    await repo.list_by_thread("thread-1")
    sql_text = str(conn.execute.call_args[0][0])
    assert "create_user" not in sql_text


# ---------------------------------------------------------------------------
# list_by_user
# ---------------------------------------------------------------------------


async def test_list_by_user_returns_rows():
    rows = [_row(attachment_id=f"ATT{i}", create_user="alice") for i in range(3)]
    engine, _ = _mock_engine(rows=rows)
    repo = AttachmentRepository(engine)
    results = await repo.list_by_user("alice")
    assert len(results) == 3
    assert all(isinstance(r, AttachmentRow) for r in results)


async def test_list_by_user_filters_by_create_user():
    engine, conn = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    await repo.list_by_user("alice")
    sql_text = str(conn.execute.call_args[0][0])
    params = conn.execute.call_args[0][1]
    assert "create_user" in sql_text
    assert params["create_user"] == "alice"


async def test_list_by_user_after_cursor_filters():
    row = _row(attachment_id="ATT5", create_user="alice")
    engine, conn = _mock_engine(rows=[row])
    repo = AttachmentRepository(engine)
    results = await repo.list_by_user("alice", after="ATT9", limit=5)
    assert results[0].attachment_id == "ATT5"
    sql_text = str(conn.execute.call_args[0][0])
    assert "< :after" in sql_text


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_executes_artifact_and_attachment_deletes():
    engine, conn = _mock_engine()
    repo = AttachmentRepository(engine)
    await repo.delete("ATT001")
    assert conn.execute.call_count == 2
    first_sql = str(conn.execute.call_args_list[0][0][0])
    second_sql = str(conn.execute.call_args_list[1][0][0])
    assert "chat_attachment_artifacts" in first_sql
    assert "chat_attachments" in second_sql and "chat_attachment_artifacts" not in second_sql
    assert conn.execute.call_args_list[0][0][1] == {"id": "ATT001"}
    assert conn.execute.call_args_list[1][0][1] == {"id": "ATT001"}


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


async def test_update_status_executes_update():
    engine, conn = _mock_engine()
    repo = AttachmentRepository(engine)
    await repo.update_status("ATT001", "READY")
    conn.execute.assert_called_once()
    params = conn.execute.call_args[0][1]
    assert params["status"] == "READY"
    assert params["id"] == "ATT001"


async def test_update_status_persists_error_code_and_reason():
    engine, conn = _mock_engine()
    repo = AttachmentRepository(engine)
    await repo.update_status("ATT001", "FAILED", error_code="EMBEDDER_ERROR", error_reason="boom")
    params = conn.execute.call_args[0][1]
    assert params["error_code"] == "EMBEDDER_ERROR"
    assert params["error_reason"] == "boom"


async def test_update_status_defaults_error_fields_to_none():
    engine, conn = _mock_engine()
    repo = AttachmentRepository(engine)
    await repo.update_status("ATT001", "READY")
    params = conn.execute.call_args[0][1]
    assert params["error_code"] is None
    assert params["error_reason"] is None


# ---------------------------------------------------------------------------
# claim_for_processing
# ---------------------------------------------------------------------------


async def test_claim_for_processing_happy_path_returns_pre_state():
    pre_row = _row(attachment_id="ATT001", status="UPLOADED")
    engine, conn = _mock_claim_engine(pre_row, update_rowcount=1)
    repo = AttachmentRepository(engine)

    claimed = await repo.claim_for_processing("ATT001")

    assert isinstance(claimed, AttachmentRow)
    assert claimed.attachment_id == "ATT001"
    assert claimed.status == "UPLOADED"
    assert conn.execute.call_count == 2


async def test_claim_for_processing_returns_none_on_lost_race():
    pre_row = _row(attachment_id="ATT001", status="UPLOADED")
    engine, _ = _mock_claim_engine(pre_row, update_rowcount=0)
    repo = AttachmentRepository(engine)

    assert await repo.claim_for_processing("ATT001") is None


async def test_claim_for_processing_returns_none_for_terminal_status():
    pre_row = _row(attachment_id="ATT001", status="READY")
    engine, conn = _mock_claim_engine(pre_row, update_rowcount=1)
    repo = AttachmentRepository(engine)

    assert await repo.claim_for_processing("ATT001") is None
    conn.execute.assert_called_once()


async def test_claim_for_processing_returns_none_for_missing_row():
    engine, conn = _mock_claim_engine(pre_row=None, update_rowcount=1)
    repo = AttachmentRepository(engine)

    assert await repo.claim_for_processing("MISSING") is None
    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# add_artifact
# ---------------------------------------------------------------------------


async def test_add_artifact_executes_insert():
    engine, conn = _mock_engine()
    repo = AttachmentRepository(engine)
    await repo.add_artifact(
        "ATT001",
        "complete",
        "chat_attachments/ATT001/complete.json",
        "text/markdown",
        char_count=120,
    )
    conn.execute.assert_called_once()
    params = conn.execute.call_args[0][1]
    assert params["attachment_id"] == "ATT001"
    assert params["variant"] == "complete"
    assert params["storage_key"] == "chat_attachments/ATT001/complete.json"
    assert params["content_type"] == "text/markdown"
    assert params["char_count"] == 120


# ---------------------------------------------------------------------------
# get_artifacts
# ---------------------------------------------------------------------------


async def test_get_artifacts_returns_rows():
    rows = [
        _artifact_row(variant="complete", storage_key="key-complete"),
        _artifact_row(variant="simplified", storage_key="key-simplified"),
    ]
    engine, _ = _mock_engine(rows=rows)
    repo = AttachmentRepository(engine)
    results = await repo.get_artifacts("ATT001")
    assert len(results) == 2
    assert all(isinstance(r, ArtifactRow) for r in results)
    assert {r.variant for r in results} == {"complete", "simplified"}


async def test_get_artifacts_returns_empty_for_missing():
    engine, _ = _mock_engine(rows=[])
    repo = AttachmentRepository(engine)
    assert await repo.get_artifacts("MISSING") == []
