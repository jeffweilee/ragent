"""T-EM.6 — SystemSettingsRepository: typed key/JSON CRUD (B50).

Wraps the `system_settings` table introduced in migration 009. Generic enough
to host any future runtime-mutable setting; today's caller is the embedding
lifecycle service.

Contract:
- `get(key)` returns JSON-decoded value, or None when key absent.
- `set(key, value)` upserts atomically (INSERT ... ON DUPLICATE KEY UPDATE).
- `transition(updates)` writes multiple keys in a single transaction —
  used by lifecycle transitions so partial failures cannot leave the
  state machine in an impossible state.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_engine(first_row=None, rowcount=1):
    result = MagicMock()
    result.mappings.return_value.first.return_value = first_row
    result.rowcount = rowcount

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    return engine, conn


async def test_get_returns_parsed_json_dict() -> None:
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, conn = _mock_engine(first_row={"setting_value": '{"name":"bge-m3","dim":1024}'})
    repo = SystemSettingsRepository(engine)

    value = await repo.get("embedding.stable")

    assert value == {"name": "bge-m3", "dim": 1024}
    conn.execute.assert_awaited_once()


async def test_get_returns_parsed_json_string() -> None:
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, _ = _mock_engine(first_row={"setting_value": '"stable"'})
    repo = SystemSettingsRepository(engine)

    assert await repo.get("embedding.read") == "stable"


async def test_get_returns_parsed_json_null() -> None:
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, _ = _mock_engine(first_row={"setting_value": "null"})
    repo = SystemSettingsRepository(engine)

    assert await repo.get("embedding.candidate") is None


async def test_get_returns_none_when_key_missing() -> None:
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, _ = _mock_engine(first_row=None)
    repo = SystemSettingsRepository(engine)

    assert await repo.get("nonexistent.key") is None


async def test_get_passes_dict_through_when_driver_already_decoded() -> None:
    """MariaDB JSON columns may arrive pre-decoded depending on driver."""
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, _ = _mock_engine(first_row={"setting_value": {"name": "bge-m3", "dim": 1024}})
    repo = SystemSettingsRepository(engine)

    assert await repo.get("embedding.stable") == {"name": "bge-m3", "dim": 1024}


async def test_set_emits_upsert_with_json_payload() -> None:
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, conn = _mock_engine()
    repo = SystemSettingsRepository(engine)

    await repo.set("embedding.candidate", {"name": "bge-m3-v2", "dim": 768})

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args
    params = call_args[0][1]
    assert params["key"] == "embedding.candidate"
    # Value is JSON-encoded so MariaDB stores the literal JSON; the SQL
    # uses CAST(:value AS JSON) on the application side.
    import json as _json

    assert _json.loads(params["value"]) == {"name": "bge-m3-v2", "dim": 768}


async def test_set_handles_none_value() -> None:
    """Setting candidate to null is the abort/commit move."""
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, conn = _mock_engine()
    repo = SystemSettingsRepository(engine)

    await repo.set("embedding.candidate", None)

    params = conn.execute.call_args[0][1]
    import json as _json

    assert _json.loads(params["value"]) is None


async def test_transition_writes_multiple_keys_in_one_transaction() -> None:
    """Cutover/rollback/commit/abort must be atomic across keys."""
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, conn = _mock_engine()
    repo = SystemSettingsRepository(engine)

    await repo.transition(
        {
            "embedding.candidate": None,
            "embedding.retired": [{"name": "old", "dim": 1024}],
        }
    )

    # One `begin()` context = one transaction; multiple execute calls inside it.
    assert engine.begin.call_count == 1
    assert conn.execute.await_count == 2


def _mock_engine_with_select_for_update(select_rows: list[dict]):
    """Build a mock engine whose first execute returns `select_rows` for the
    SELECT FOR UPDATE preflight; subsequent executes are no-op upserts."""
    select_result = MagicMock()
    select_result.mappings.return_value.all.return_value = select_rows
    upsert_result = MagicMock()
    upsert_result.rowcount = 1

    conn = AsyncMock()
    conn.execute = AsyncMock(
        side_effect=[select_result, upsert_result, upsert_result, upsert_result]
    )

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    return engine, conn


async def test_transition_with_expect_passes_when_live_matches() -> None:
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, conn = _mock_engine_with_select_for_update(
        [{"setting_key": "embedding.read", "setting_value": '"stable"'}]
    )
    repo = SystemSettingsRepository(engine)

    await repo.transition(
        {"embedding.read": "candidate"},
        expect={"embedding.read": "stable"},
    )

    # SELECT FOR UPDATE + 1 upsert = 2 execute calls.
    assert conn.execute.await_count == 2


async def test_transition_with_expect_raises_when_live_drifted() -> None:
    """Concurrent admin actions racing — second writer's snapshot is stale."""
    from ragent.repositories.system_settings_repository import (
        OptimisticLockMismatch,
        SystemSettingsRepository,
    )

    engine, conn = _mock_engine_with_select_for_update(
        [{"setting_key": "embedding.read", "setting_value": '"candidate"'}]  # already flipped
    )
    repo = SystemSettingsRepository(engine)

    with pytest.raises(OptimisticLockMismatch):
        await repo.transition(
            {"embedding.read": "candidate"},
            expect={"embedding.read": "stable"},  # stale snapshot
        )

    # Only the SELECT FOR UPDATE fired; no upsert because we raised.
    assert conn.execute.await_count == 1


async def test_transition_without_expect_skips_select_for_update() -> None:
    """Back-compat: expect=None preserves the original behaviour."""
    from ragent.repositories.system_settings_repository import SystemSettingsRepository

    engine, conn = _mock_engine()
    repo = SystemSettingsRepository(engine)

    await repo.transition({"embedding.read": "stable"})

    assert conn.execute.await_count == 1  # one upsert, no SELECT
