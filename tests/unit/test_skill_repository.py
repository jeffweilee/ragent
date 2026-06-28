"""T-SK — SkillRepository owner-scoping and SQL-shape contracts (mocked engine).

The security property under test: EVERY read/write statement filters by
``user_id``, and ``user_id`` is always among the bound parameters. A repository
method that could touch a row by ``skill_id`` alone would be a cross-user leak,
so these tests assert the WHERE clause and the params, not just the happy path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.skill_repository import SkillRepository


def _mock_engine(*, first=None, all_rows=None, rowcount=1):
    result = MagicMock()
    result.rowcount = rowcount
    result.mappings.return_value.first.return_value = first
    result.mappings.return_value.all.return_value = all_rows or []

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
        "skill_id": "SKILL000000000000000000000",
        "user_id": "alice",
        "name": "Translator",
        "description": "",
        "instructions": "Translate to English.",
        "enabled": 1,
        "created_at": datetime(2026, 6, 24, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 24, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


async def test_create_inserts_and_returns_26_char_id():
    engine, conn = _mock_engine()
    repo = SkillRepository(engine)
    sid = await repo.create(
        user_id="alice", name="Translator", description="", instructions="x", enabled=True
    )
    assert isinstance(sid, str) and len(sid) == 26
    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "INSERT INTO skills" in sql
    assert params["user_id"] == "alice"
    assert params["skill_id"] == sid


async def test_get_filters_by_user_id_and_skill_id():
    engine, conn = _mock_engine(first=_row())
    repo = SkillRepository(engine)
    row = await repo.get(user_id="alice", skill_id="SKILL000000000000000000000")
    assert row is not None
    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "WHERE user_id = :user_id AND skill_id = :skill_id" in sql
    assert params == {"user_id": "alice", "skill_id": "SKILL000000000000000000000"}


async def test_get_returns_none_when_absent():
    engine, _ = _mock_engine(first=None)
    repo = SkillRepository(engine)
    assert await repo.get(user_id="alice", skill_id="missing") is None


async def test_list_filters_by_user_id_and_orders_desc():
    engine, conn = _mock_engine(all_rows=[_row(), _row(skill_id="SKILL000000000000000000001")])
    repo = SkillRepository(engine)
    rows = await repo.list(user_id="alice")
    assert len(rows) == 2
    sql = str(conn.execute.call_args.args[0])
    assert "WHERE user_id = :user_id" in sql
    assert "ORDER BY created_at DESC" in sql
    assert conn.execute.call_args.args[1] == {"user_id": "alice"}


async def test_update_scopes_to_owner_and_returns_rowcount():
    engine, conn = _mock_engine(rowcount=1)
    repo = SkillRepository(engine)
    rc = await repo.update(
        user_id="alice",
        skill_id="SKILL000000000000000000000",
        name="n",
        description="d",
        instructions="i",
        enabled=False,
    )
    assert rc == 1
    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "WHERE user_id = :user_id AND skill_id = :skill_id" in sql
    assert params["user_id"] == "alice"
    assert params["enabled"] is False


async def test_update_rowcount_zero_when_not_owned():
    engine, _ = _mock_engine(rowcount=0)
    repo = SkillRepository(engine)
    rc = await repo.update(
        user_id="bob",
        skill_id="SKILL000000000000000000000",
        name="n",
        description="d",
        instructions="i",
        enabled=True,
    )
    assert rc == 0


async def test_delete_scopes_to_owner_and_returns_rowcount():
    engine, conn = _mock_engine(rowcount=1)
    repo = SkillRepository(engine)
    rc = await repo.delete(user_id="alice", skill_id="SKILL000000000000000000000")
    assert rc == 1
    sql = str(conn.execute.call_args.args[0])
    assert "DELETE FROM skills WHERE user_id = :user_id AND skill_id = :skill_id" in sql
    assert conn.execute.call_args.args[1] == {
        "user_id": "alice",
        "skill_id": "SKILL000000000000000000000",
    }
