"""T-FB.4 — FeedbackRepository.upsert idempotency on (user, request, app, source) quadruple."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.repositories.feedback_repository import FeedbackRepository


def _mock_engine(rowcount: int = 1):
    result = MagicMock()
    result.rowcount = rowcount

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    return engine, conn


async def test_upsert_returns_feedback_id_charsel():
    engine, _ = _mock_engine()
    repo = FeedbackRepository(engine)
    fid = await repo.upsert(
        request_id="REQUESTIDREQUESTIDREQUESTID",
        user_id="alice",
        source_app="confluence",
        source_id="DOC-1",
        vote=1,
        reason="irrelevant",
        position_shown=2,
    )
    assert isinstance(fid, str) and len(fid) == 26


async def test_upsert_executes_insert_on_duplicate_key_update():
    engine, conn = _mock_engine()
    repo = FeedbackRepository(engine)
    await repo.upsert(
        request_id="REQ",
        user_id="alice",
        source_app="confluence",
        source_id="S1",
        vote=1,
        reason=None,
    )
    sql = str(conn.execute.call_args.args[0])
    assert "INSERT INTO feedback" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql.upper() or "ON DUPLICATE" in sql.upper()


async def test_upsert_binds_source_app_and_source_id_into_sql_params():
    """Both source_app and source_id appear in the bound parameters (pair identity)."""
    engine, conn = _mock_engine()
    repo = FeedbackRepository(engine)
    await repo.upsert(
        request_id="R",
        user_id="u",
        source_app="drive",
        source_id="s",
        vote=1,
        reason=None,
    )
    params = conn.execute.call_args.args[1]
    assert params["source_app"] == "drive"
    assert params["source_id"] == "s"


async def test_upsert_accepts_null_reason():
    engine, conn = _mock_engine()
    repo = FeedbackRepository(engine)
    await repo.upsert(
        request_id="R", user_id="u", source_app="confluence", source_id="s", vote=-1, reason=None
    )
    params = conn.execute.call_args.args[1]
    assert params["reason"] is None


async def test_upsert_rejects_vote_outside_unit():
    engine, _ = _mock_engine()
    repo = FeedbackRepository(engine)
    with pytest.raises(ValueError):
        await repo.upsert(
            request_id="R",
            user_id="u",
            source_app="confluence",
            source_id="s",
            vote=0,
            reason=None,
        )
    with pytest.raises(ValueError):
        await repo.upsert(
            request_id="R",
            user_id="u",
            source_app="confluence",
            source_id="s",
            vote=2,
            reason=None,
        )


async def test_upsert_propagates_position_shown_when_supplied():
    engine, conn = _mock_engine()
    repo = FeedbackRepository(engine)
    await repo.upsert(
        request_id="R",
        user_id="u",
        source_app="confluence",
        source_id="s",
        vote=1,
        reason="incomplete",
        position_shown=5,
    )
    assert conn.execute.call_args.args[1]["position_shown"] == 5


async def test_upsert_position_shown_defaults_to_none():
    engine, conn = _mock_engine()
    repo = FeedbackRepository(engine)
    await repo.upsert(
        request_id="R",
        user_id="u",
        source_app="confluence",
        source_id="s",
        vote=1,
        reason="other",
    )
    assert conn.execute.call_args.args[1]["position_shown"] is None


async def test_upsert_sets_created_and_updated_at_to_same_value():
    """Insert path: created_at and updated_at must match (DB triggers don't fire in upsert)."""
    engine, conn = _mock_engine()
    repo = FeedbackRepository(engine)
    await repo.upsert(
        request_id="R", user_id="u", source_app="confluence", source_id="s", vote=1, reason=None
    )
    params = conn.execute.call_args.args[1]
    assert params["created_at"] == params["updated_at"]
