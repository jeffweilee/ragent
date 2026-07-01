"""T-SK presets — SkillService merges built-in presets and protects them."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from ragent.services.skill_presets import PRESET_BY_ID, PRESETS
from ragent.services.skill_service import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillReadOnlyError,
    SkillService,
)

PRESET_ID = "skill-manager"


def _row(**over):
    base = {
        "skill_id": "SKILL000000000000000000000",
        "user_id": "alice",
        "name": "Mine",
        "description": "",
        "instructions": "do a thing",
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


def test_skill_manager_preset_is_registered():
    assert PRESET_ID in PRESET_BY_ID
    p = PRESET_BY_ID[PRESET_ID]
    assert p.name == "skill-manager"
    assert p.enabled is True
    # the persona lists the full CRUD tool family so the agent calls them
    for tool in ("create_skill", "list_skills", "get_skill", "update_skill", "delete_skill"):
        assert tool in p.instructions


async def test_list_pins_presets_before_user_skills():
    svc = SkillService(_repo(list=AsyncMock(return_value=[_row()])))
    out = await svc.list_for_user(user_id="alice")
    assert out[0].skill_id == PRESET_ID  # preset first
    assert [s.skill_id for s in out[1:]] == ["SKILL000000000000000000000"]


async def test_preset_is_readonly_user_skill_is_not():
    repo = _repo(get=AsyncMock(return_value=_row()))
    svc = SkillService(repo)
    assert (await svc.get(user_id="alice", skill_id=PRESET_ID)).readonly is True
    assert (await svc.get(user_id="alice", skill_id="SKILL000000000000000000000")).readonly is False


async def test_get_preset_without_touching_repo():
    repo = _repo(get=AsyncMock(return_value=None))
    svc = SkillService(repo)
    resp = await svc.get(user_id="alice", skill_id=PRESET_ID)
    assert resp.skill_id == PRESET_ID
    repo.get.assert_not_called()  # preset short-circuits the DB


async def test_resolve_preset_returns_its_instructions():
    repo = _repo(get=AsyncMock(return_value=None))
    svc = SkillService(repo)
    text = await svc.resolve_instructions(user_id="alice", skill_id=PRESET_ID)
    assert text == PRESET_BY_ID[PRESET_ID].instructions
    repo.get.assert_not_called()


async def test_create_with_preset_name_conflicts():
    svc = SkillService(_repo(create=AsyncMock()))
    with pytest.raises(SkillNameConflictError):
        await svc.create(
            user_id="alice",
            name="skill-manager",
            description="",
            instructions="x",
            enabled=True,
        )


async def test_create_with_preset_name_conflicts_case_insensitive():
    # "Skill-Manager" must not shadow the built-in "skill-manager".
    svc = SkillService(_repo(create=AsyncMock()))
    with pytest.raises(SkillNameConflictError):
        await svc.create(
            user_id="alice",
            name="Skill-Manager",
            description="",
            instructions="x",
            enabled=True,
        )


async def test_update_foreign_or_missing_id_with_reserved_name_is_404_not_409():
    # PUT to a non-preset id the caller doesn't own + a reserved name must return
    # 404 (ownership checked first), not a 409 leaking the reserved-name verdict.
    repo = _repo(get=AsyncMock(return_value=None), update=AsyncMock(return_value=0))
    svc = SkillService(repo)
    with pytest.raises(SkillNotFoundError):
        await svc.update(
            user_id="alice",
            skill_id="SKILL000000000000000000000",
            name="skill-manager",
            description="",
            instructions="x",
            enabled=True,
        )
    repo.update.assert_not_called()  # never written with the reserved name


async def test_update_preset_is_read_only():
    svc = SkillService(_repo(update=AsyncMock()))
    with pytest.raises(SkillReadOnlyError):
        await svc.update(
            user_id="alice",
            skill_id=PRESET_ID,
            name="whatever",
            description="",
            instructions="x",
            enabled=True,
        )


async def test_delete_preset_is_read_only():
    svc = SkillService(_repo(delete=AsyncMock()))
    with pytest.raises(SkillReadOnlyError):
        await svc.delete(user_id="alice", skill_id=PRESET_ID)


async def test_non_preset_get_still_hits_repo():
    repo = _repo(get=AsyncMock(return_value=None))
    svc = SkillService(repo)
    with pytest.raises(SkillNotFoundError):
        await svc.get(user_id="alice", skill_id="SKILL000000000000000000000")
    repo.get.assert_awaited_once()


def test_presets_are_frozen_dataclasses():
    # presets must not be mutated at runtime (read-only built-ins).
    with pytest.raises(AttributeError):
        PRESETS[0].name = "x"  # type: ignore[misc]
