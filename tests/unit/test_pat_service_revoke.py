"""T-PAT.26 — PatService.revoke: hard delete + tombstoned cache invalidation.

The interleaving tests here are the reason the tombstone exists. Ordering the DB
delete before the eviction is NOT sufficient on its own: `_do_refresh` awaits
between its DB write and its `cache.put`, so an entire revoke can be scheduled
in that gap on a single event loop, and the rotation would then repopulate redis
for a row that no longer exists. Because cache hits never consult the DB, that
republished credential would keep riding every brain call for the full TTL.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from ragent.services.pat_service import PatReauthRequired
from tests.unit.pat_fakes import FakeRefreshClient, build_service, sign


def _seed(repo, cache, cipher, nt="alice", *, exp_delta=3600):
    blob = cipher.encrypt(sign(nt, exp_delta=exp_delta))
    repo.rows[nt] = {"user_id": nt, "pat_cipher": blob, "status": "active"}
    cache.put(nt, blob)
    return blob


async def test_revoke_deletes_row_and_clears_cache() -> None:
    service, repo, cache, cipher = build_service()
    _seed(repo, cache, cipher)

    await service.revoke(nt="alice")

    assert "alice" not in repo.rows
    assert cache.get("alice") is None


async def test_revoke_is_idempotent_when_never_authorized() -> None:
    service, repo, cache, _ = build_service()

    await service.revoke(nt="ghost")  # must not raise

    assert repo.rows == {}
    assert cache.get("ghost") is None


async def test_revoke_logs_entry_and_exit_with_existed_flag() -> None:
    service, repo, cache, cipher = build_service()
    _seed(repo, cache, cipher)

    with capture_logs() as captured:
        await service.revoke(nt="alice")
        await service.revoke(nt="alice")  # second time: nothing left to delete

    completed = [e for e in captured if e.get("event") == "pat.revoke.completed"]
    assert [e.get("event") for e in captured].count("pat.revoke.started") == 2
    assert [e["existed"] for e in completed] == [True, False]
    assert completed[0]["user_id"] == "alice"


async def test_db_delete_happens_before_the_cache_eviction() -> None:
    # Evicting first would let a concurrent resolve miss the cache, read the
    # still-present row and re-fill redis. Assert the ORDER, not just that both ran.
    service, repo, cache, cipher = build_service()
    _seed(repo, cache, cipher)
    order: list[str] = []

    original_delete = repo.delete if hasattr(repo, "delete") else None
    assert original_delete is not None

    async def recording_delete(*, user_id: str) -> int:
        order.append("db_delete")
        return await original_delete(user_id=user_id)

    original_evict = cache.evict

    def recording_evict(nt: str) -> None:
        order.append("cache_evict")
        original_evict(nt)

    repo.delete = recording_delete
    cache.evict = recording_evict

    await service.revoke(nt="alice")

    assert order == ["db_delete", "cache_evict"]


async def test_rotation_landing_after_a_revoke_cannot_repopulate_the_cache() -> None:
    """The interleaving the tombstone exists for.

    `rotate` succeeds (rowcount 1, so T-PAT.24's check does NOT fire), then the
    revoke completes, then the refresh reaches its `cache.put`. The cache must
    end up EMPTY — that is the whole point, and ordering alone cannot achieve it.

    This one request still returns its rotated token: it was already in flight
    and the credential is in its memory either way, so failing it would close
    nothing. What must not happen is the token outliving the request by being
    republished to a cache that every *subsequent* request reads without ever
    consulting the DB.
    """
    rotated = sign("alice")
    service, repo, cache, cipher = build_service(refresh_client=FakeRefreshClient([rotated]))
    _seed(repo, cache, cipher, exp_delta=-10)
    original_rotate = repo.rotate

    async def rotate_then_revoke(*, user_id: str, pat_cipher: str) -> int:
        rowcount = await original_rotate(user_id=user_id, pat_cipher=pat_cipher)
        await service.revoke(nt=user_id)  # the revoke lands in the await gap
        return rowcount

    repo.rotate = rotate_then_revoke

    assert await service.resolve("alice") == rotated  # this request completes

    assert "alice" not in repo.rows  # the revoke stands
    assert cache.get("alice") is None  # and nothing was republished
    # Proof the next request is locked out rather than served from a stale cache.
    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")


async def test_resolve_refill_after_a_revoke_cannot_repopulate_the_cache() -> None:
    # The other repopulation path: resolve's cache-miss re-fill. Guarded by the
    # same tombstone because the check lives in PatCache.put, not at call sites.
    service, repo, cache, cipher = build_service()
    blob = _seed(repo, cache, cipher)
    await service.revoke(nt="alice")

    cache.put("alice", blob)  # a straggler re-fill from a resolve already in flight

    assert cache.get("alice") is None
