"""T-PAT.14 — PatRepository against real MariaDB (testcontainers).

Proves the one-PAT-per-user invariant + overwrite/reactivate semantics are
enforced by the schema (migration 017), not merely application code:
  * `user_id` is UNIQUE — a second authorize overwrites in place (one row);
  * a successful refresh / re-authorization resets an invalid row to active;
  * `mark_invalid` flips only the caller's row.
"""

from __future__ import annotations

from datetime import date

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


async def test_rotate_updates_in_place_and_leaves_the_window_alone(repo):
    # T-PAT.24/25 against real MariaDB: refresh overwrites the token but must not
    # move the authorization window — only authorize may do that.
    pat_repo, engine = repo
    user = f"alice-{new_id()}"
    await pat_repo.upsert(
        user_id=user, pat_cipher="v1.a.a", authorization_expires_at=date(2027, 7, 29)
    )
    authorized_at = (await pat_repo.get(user_id=user))["authorized_at"]

    assert await pat_repo.rotate(user_id=user, pat_cipher="v1.b.b") == 1

    row = await pat_repo.get(user_id=user)
    assert await _count(engine, user) == 1
    assert row["pat_cipher"] == "v1.b.b"
    assert row["authorization_expires_at"] == date(2027, 7, 29)
    assert row["authorized_at"] == authorized_at


async def test_rotate_does_not_recreate_a_revoked_row(repo):
    # The revoke-resurrection guard: UPDATE on a missing row affects 0 rows and
    # must NOT insert one.
    pat_repo, engine = repo
    user = f"ghost-{new_id()}"

    assert await pat_repo.rotate(user_id=user, pat_cipher="v1.a.a") == 0
    assert await _count(engine, user) == 0


async def test_reauthorize_restarts_the_window(repo):
    pat_repo, _ = repo
    user = f"carol-{new_id()}"

    await pat_repo.upsert(
        user_id=user, pat_cipher="v1.a.a", authorization_expires_at=date(2027, 1, 1)
    )
    await pat_repo.upsert(
        user_id=user, pat_cipher="v1.b.b", authorization_expires_at=date(2028, 1, 1)
    )

    assert (await pat_repo.get(user_id=user))["authorization_expires_at"] == date(2028, 1, 1)


async def test_window_is_null_when_not_supplied(repo):
    # Rows written before 018 (and any caller that omits it) read back as
    # "unknown" rather than a fabricated date.
    pat_repo, _ = repo
    user = f"dave-{new_id()}"

    await pat_repo.upsert(user_id=user, pat_cipher="v1.a.a")

    assert (await pat_repo.get(user_id=user))["authorization_expires_at"] is None


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
