"""T-PAT.14 — PatRepository against real MariaDB (testcontainers).

Proves the one-PAT-per-user invariant + overwrite/reactivate semantics are
enforced by the schema (migration 017), not merely application code:
  * `user_id` is UNIQUE — a second authorize overwrites in place (one row);
  * a successful refresh / re-authorization resets an invalid row to active;
  * `mark_invalid` flips only the caller's row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from ragent.bootstrap.init_schema import init_mariadb, to_sync_dsn
from ragent.repositories.pat_repository import PatRepository
from ragent.utility.id_gen import new_id

pytestmark = pytest.mark.docker


@pytest.fixture
async def repo(mariadb_dsn: str):
    init_mariadb(create_engine(to_sync_dsn(mariadb_dsn)))
    engine = create_async_engine(mariadb_dsn)
    try:
        yield PatRepository(engine), engine
    finally:
        await engine.dispose()


async def _count(engine, user_id: str) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM pat WHERE user_id = :u"), {"u": user_id}
        )
        return int(result.scalar_one())


async def test_upsert_is_one_row_per_user(repo):
    pat_repo, engine = repo
    user = f"alice-{new_id()}"

    await pat_repo.upsert(user_id=user, pat_cipher="v1.a.a")
    await pat_repo.upsert(user_id=user, pat_cipher="v1.b.b")  # overwrite

    assert await _count(engine, user) == 1
    row = await pat_repo.get(user_id=user)
    assert row["pat_cipher"] == "v1.b.b"
    assert row["status"] == "active"


async def test_mark_invalid_then_reauthorize_reactivates(repo):
    pat_repo, _ = repo
    user = f"bob-{new_id()}"

    await pat_repo.upsert(user_id=user, pat_cipher="v1.a.a")
    assert await pat_repo.mark_invalid(user_id=user) == 1
    assert (await pat_repo.get(user_id=user))["status"] == "invalid"

    # A successful refresh / re-authorization overwrites and reactivates.
    await pat_repo.upsert(user_id=user, pat_cipher="v1.c.c")
    row = await pat_repo.get(user_id=user)
    assert row["status"] == "active"
    assert row["pat_cipher"] == "v1.c.c"


async def test_mark_invalid_scoped_to_caller(repo):
    pat_repo, _ = repo
    alice = f"alice-{new_id()}"
    bob = f"bob-{new_id()}"
    await pat_repo.upsert(user_id=alice, pat_cipher="v1.a.a")
    await pat_repo.upsert(user_id=bob, pat_cipher="v1.b.b")

    await pat_repo.mark_invalid(user_id=alice)

    assert (await pat_repo.get(user_id=alice))["status"] == "invalid"
    assert (await pat_repo.get(user_id=bob))["status"] == "active"


async def test_get_absent_returns_none(repo):
    pat_repo, _ = repo
    assert await pat_repo.get(user_id=f"ghost-{new_id()}") is None
