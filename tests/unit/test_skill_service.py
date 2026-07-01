"""T-SK — SkillService business logic, error mapping, and boundary logs."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from ragent.services.skill_service import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillService,
)


def _row(**over):
    base = {
        "skill_id": "SKILL000000000000000000000",
        "user_id": "alice",
        "name": "Translator",
        "description": "to English",
        "instructions": "Translate everything to English.",
        "enabled": 1,
        "created_at": datetime(2026, 6, 24, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 24, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


def _repo(**methods):
    repo = AsyncMock()
    for name, value in methods.items():
        setattr(repo, name, value)
    return repo


async def test_create_returns_response_with_iso_timestamps():
    repo = _repo(
        create=AsyncMock(return_value="SKILL000000000000000000000"),
        get=AsyncMock(return_value=_row()),
    )
    svc = SkillService(repo)
    resp = await svc.create(
        user_id="alice", name="Translator", description="to English", instructions="x", enabled=True
    )
    assert resp.skill_id == "SKILL000000000000000000000"
    assert resp.enabled is True
    assert resp.created_at.endswith("+00:00") or resp.created_at.endswith("Z")


async def test_create_duplicate_name_maps_to_conflict():
    err = IntegrityError("INSERT", {}, Exception("Duplicate entry"))
    svc = SkillService(_repo(create=AsyncMock(side_effect=err)))
    with pytest.raises(SkillNameConflictError):
        await svc.create(
            user_id="alice", name="dup", description="", instructions="x", enabled=True
        )


async def test_get_missing_raises_not_found():
    svc = SkillService(_repo(get=AsyncMock(return_value=None)))
    with pytest.raises(SkillNotFoundError):
        await svc.get(user_id="alice", skill_id="missing")


async def test_list_maps_rows():
    svc = SkillService(_repo(list=AsyncMock(return_value=[_row(), _row()])))
    out = await svc.list_for_user(user_id="alice")
    # 1 built-in preset (skill-manager) is pinned ahead of the 2 user skills.
    assert len(out) == 3
    assert out[0].skill_id == "skill-manager"


async def test_update_rowcount_zero_raises_not_found():
    svc = SkillService(_repo(update=AsyncMock(return_value=0)))
    with pytest.raises(SkillNotFoundError):
        await svc.update(
            user_id="alice",
            skill_id="x",
            name="n",
            description="d",
            instructions="i",
            enabled=True,
        )


async def test_update_success_returns_refreshed_response():
    repo = _repo(update=AsyncMock(return_value=1), get=AsyncMock(return_value=_row(name="New")))
    svc = SkillService(repo)
    resp = await svc.update(
        user_id="alice",
        skill_id="SKILL000000000000000000000",
        name="New",
        description="d",
        instructions="i",
        enabled=True,
    )
    assert resp.name == "New"


async def test_update_duplicate_name_maps_to_conflict():
    err = IntegrityError("UPDATE", {}, Exception("Duplicate entry"))
    svc = SkillService(_repo(update=AsyncMock(side_effect=err)))
    with pytest.raises(SkillNameConflictError):
        await svc.update(
            user_id="alice",
            skill_id="x",
            name="dup",
            description="d",
            instructions="i",
            enabled=True,
        )


async def test_delete_rowcount_zero_raises_not_found():
    svc = SkillService(_repo(delete=AsyncMock(return_value=0)))
    with pytest.raises(SkillNotFoundError):
        await svc.delete(user_id="alice", skill_id="missing")


async def test_resolve_instructions_returns_enabled_skill_text():
    svc = SkillService(_repo(get=AsyncMock(return_value=_row())))
    text = await svc.resolve_instructions(user_id="alice", skill_id="SKILL000000000000000000000")
    assert text == "Translate everything to English."


async def test_resolve_instructions_disabled_raises_not_found():
    svc = SkillService(_repo(get=AsyncMock(return_value=_row(enabled=0))))
    with pytest.raises(SkillNotFoundError):
        await svc.resolve_instructions(user_id="alice", skill_id="SKILL000000000000000000000")


async def test_resolve_instructions_missing_raises_not_found():
    svc = SkillService(_repo(get=AsyncMock(return_value=None)))
    with pytest.raises(SkillNotFoundError):
        await svc.resolve_instructions(user_id="alice", skill_id="missing")


async def test_create_emits_entry_and_exit_logs_without_content():
    repo = _repo(
        create=AsyncMock(return_value="SKILL000000000000000000000"),
        get=AsyncMock(return_value=_row()),
    )
    svc = SkillService(repo)
    with capture_logs() as logs:
        await svc.create(
            user_id="alice", name="Translator", description="", instructions="secret", enabled=True
        )
    events = {e["event"] for e in logs}
    assert "skill.create" in events
    assert "skill.created" in events
    # identity only — never the instruction text.
    assert all("secret" not in str(e.get(k)) for e in logs for k in e)
