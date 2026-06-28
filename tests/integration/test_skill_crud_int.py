"""T-SK — SkillRepository / SkillService against real MariaDB (testcontainers).

Proves the isolation + uniqueness invariants are enforced by the database
schema (migration 013), not merely by application code:
  * a user only ever sees / mutates their own rows;
  * (user_id, name) is unique per owner;
  * a foreign owner's update/delete is a no-op (rowcount 0).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from ragent.bootstrap.init_schema import _to_sync_dsn, init_mariadb
from ragent.repositories.skill_repository import SkillRepository
from ragent.services.skill_service import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillService,
)
from ragent.utility.id_gen import new_id

pytestmark = pytest.mark.docker


@pytest.fixture
async def repo(mariadb_dsn: str):
    init_mariadb(create_engine(_to_sync_dsn(mariadb_dsn)))
    engine = create_async_engine(mariadb_dsn)
    try:
        yield SkillRepository(engine)
    finally:
        await engine.dispose()


async def test_create_get_round_trip(repo: SkillRepository):
    user = f"alice-{new_id()}"
    sid = await repo.create(
        user_id=user, name="Translator", description="d", instructions="be terse", enabled=True
    )
    row = await repo.get(user_id=user, skill_id=sid)
    assert row is not None
    assert row["name"] == "Translator"
    assert row["instructions"] == "be terse"
    assert bool(row["enabled"]) is True


async def test_list_returns_only_owner_rows(repo: SkillRepository):
    alice, bob = f"alice-{new_id()}", f"bob-{new_id()}"
    await repo.create(user_id=alice, name="A", description="", instructions="x", enabled=True)
    await repo.create(user_id=bob, name="B", description="", instructions="y", enabled=True)
    rows = await repo.list(user_id=alice)
    assert [r["name"] for r in rows] == ["A"]


async def test_foreign_owner_cannot_read(repo: SkillRepository):
    alice, bob = f"alice-{new_id()}", f"bob-{new_id()}"
    sid = await repo.create(
        user_id=alice, name="Secret", description="", instructions="x", enabled=True
    )
    assert await repo.get(user_id=bob, skill_id=sid) is None


async def test_foreign_owner_update_and_delete_are_noops(repo: SkillRepository):
    alice, bob = f"alice-{new_id()}", f"bob-{new_id()}"
    sid = await repo.create(
        user_id=alice, name="Owned", description="", instructions="x", enabled=True
    )
    assert (
        await repo.update(
            user_id=bob, skill_id=sid, name="Hijack", description="", instructions="z", enabled=True
        )
        == 0
    )
    assert await repo.delete(user_id=bob, skill_id=sid) == 0
    # alice's row is untouched.
    row = await repo.get(user_id=alice, skill_id=sid)
    assert row is not None and row["name"] == "Owned"


async def test_duplicate_name_per_owner_maps_to_conflict(repo: SkillRepository):
    svc = SkillService(repo)
    user = f"alice-{new_id()}"
    await svc.create(user_id=user, name="Dup", description="", instructions="x", enabled=True)
    with pytest.raises(SkillNameConflictError):
        await svc.create(user_id=user, name="Dup", description="", instructions="y", enabled=True)


async def test_same_name_different_owners_is_allowed(repo: SkillRepository):
    svc = SkillService(repo)
    alice, bob = f"alice-{new_id()}", f"bob-{new_id()}"
    await svc.create(user_id=alice, name="Shared", description="", instructions="x", enabled=True)
    # bob may use the same name — uniqueness is per (user_id, name).
    resp = await svc.create(
        user_id=bob, name="Shared", description="", instructions="y", enabled=True
    )
    assert resp.name == "Shared"


async def test_update_round_trip_and_resolve(repo: SkillRepository):
    svc = SkillService(repo)
    user = f"alice-{new_id()}"
    created = await svc.create(
        user_id=user, name="N", description="", instructions="old", enabled=True
    )
    await svc.update(
        user_id=user,
        skill_id=created.skill_id,
        name="N2",
        description="d",
        instructions="new",
        enabled=True,
    )
    assert await svc.resolve_instructions(user_id=user, skill_id=created.skill_id) == "new"


async def test_disabled_skill_not_resolvable(repo: SkillRepository):
    svc = SkillService(repo)
    user = f"alice-{new_id()}"
    created = await svc.create(
        user_id=user, name="Off", description="", instructions="x", enabled=False
    )
    with pytest.raises(SkillNotFoundError):
        await svc.resolve_instructions(user_id=user, skill_id=created.skill_id)
