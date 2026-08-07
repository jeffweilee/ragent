"""T-PAT.4 — PatRepository SQL-shape + owner-scoping contracts (mocked engine)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.pat_repository import PatRepository


def _mock_engine(*, first=None, rowcount=1):
    result = MagicMock()
    result.rowcount = rowcount
    result.mappings.return_value.first.return_value = first

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    engine.connect = MagicMock(return_value=ctx)
    return engine, conn


def _row(**over):
    base = {
        "user_id": "alice",
        "pat_cipher": "v1.n.c",
        "status": "active",
        "created_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


async def test_upsert_overwrites_and_reactivates():
    engine, conn = _mock_engine()
    repo = PatRepository(engine)

    await repo.upsert(user_id="alice", pat_cipher="v1.n.c")

    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "INSERT INTO pat" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "status = 'active'" in sql  # re-auth / refresh resets an invalid row
    assert params["user_id"] == "alice"
    assert params["pat_cipher"] == "v1.n.c"


async def test_rotate_updates_in_place_and_never_inserts():
    # T-PAT.24: refresh rotates an EXISTING authorization. It must not be able to
    # create one — an INSERT here would resurrect a row revoked mid-refresh.
    engine, conn = _mock_engine(rowcount=1)
    repo = PatRepository(engine)

    rc = await repo.rotate(user_id="alice", pat_cipher="v1.n.c")

    assert rc == 1
    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "UPDATE pat" in sql
    assert "INSERT" not in sql
    assert "status = 'active'" in sql
    assert "WHERE user_id = :user_id" in sql
    assert params["user_id"] == "alice"
    assert params["pat_cipher"] == "v1.n.c"


async def test_rotate_rowcount_zero_when_row_was_revoked():
    engine, _ = _mock_engine(rowcount=0)
    repo = PatRepository(engine)
    assert await repo.rotate(user_id="ghost", pat_cipher="v1.n.c") == 0


async def test_get_filters_by_user_id():
    engine, conn = _mock_engine(first=_row())
    repo = PatRepository(engine)

    row = await repo.get(user_id="alice")

    assert row is not None
    sql = str(conn.execute.call_args.args[0])
    assert "WHERE user_id = :user_id" in sql
    assert conn.execute.call_args.args[1] == {"user_id": "alice"}


async def test_get_returns_none_when_absent():
    engine, _ = _mock_engine(first=None)
    repo = PatRepository(engine)
    assert await repo.get(user_id="ghost") is None


async def test_mark_invalid_flips_status_scoped_to_owner():
    engine, conn = _mock_engine(rowcount=1)
    repo = PatRepository(engine)

    rc = await repo.mark_invalid(user_id="alice")

    assert rc == 1
    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "UPDATE pat" in sql
    assert "status = 'invalid'" in sql
    assert "WHERE user_id = :user_id" in sql
    assert params["user_id"] == "alice"


async def test_mark_invalid_rowcount_zero_when_absent():
    engine, _ = _mock_engine(rowcount=0)
    repo = PatRepository(engine)
    assert await repo.mark_invalid(user_id="ghost") == 0
