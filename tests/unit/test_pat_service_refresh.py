"""T-PAT.9 — PatService refresh state machine (401/400/429, lock, rotation)."""

from __future__ import annotations

import pytest

from ragent.clients.pat_refresh_client import (
    PatRefreshBadRequest,
    PatRefreshRateLimited,
    PatRefreshUnauthorized,
)
from ragent.services.pat_service import (
    PatInternalError,
    PatReauthRequired,
    PatRefreshExhausted,
)
from tests.unit.pat_fakes import FakeRefreshClient, build_service, sign


def _seed_expired(repo, cache, cipher, nt="alice"):
    """An expired-but-active PAT in both DB and redis → resolve() will refresh."""
    expired = cipher.encrypt(sign(nt, exp_delta=-10))
    repo.rows[nt] = {"user_id": nt, "pat_cipher": expired, "status": "active"}
    cache.put(nt, expired)


async def test_success_rotates_db_and_cache() -> None:
    new_pat = sign("alice")
    service, repo, cache, cipher = build_service(refresh_client=FakeRefreshClient([new_pat]))
    _seed_expired(repo, cache, cipher)

    assert await service.resolve("alice") == new_pat
    assert cipher.decrypt(repo.rows["alice"]["pat_cipher"]) == new_pat
    assert cipher.decrypt(cache.get("alice")) == new_pat


async def test_401_invalidates_and_clears() -> None:
    rc = FakeRefreshClient([PatRefreshUnauthorized()])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")

    assert repo.rows["alice"]["status"] == "invalid"
    assert cache.get("alice") is None
    assert repo.mark_invalid_calls == ["alice"]


async def test_429_then_success_within_budget() -> None:
    new_pat = sign("alice")
    rc = FakeRefreshClient([PatRefreshRateLimited(), PatRefreshRateLimited(), new_pat])
    service, repo, cache, cipher = build_service(refresh_client=rc, max_retries=3)
    _seed_expired(repo, cache, cipher)

    assert await service.resolve("alice") == new_pat
    assert len(rc.calls) == 3  # two 429s then success


async def test_429_exhausted_rejects_but_keeps_active() -> None:
    rc = FakeRefreshClient([PatRefreshRateLimited()] * 10)
    service, repo, cache, cipher = build_service(refresh_client=rc, max_retries=3)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatRefreshExhausted):
        await service.resolve("alice")

    # rate-limit must NEVER invalidate the user's authorization.
    assert repo.rows["alice"]["status"] == "active"
    assert repo.mark_invalid_calls == []
    assert len(rc.calls) == 4  # initial + 3 retries


async def test_400_is_internal_error_and_leaves_state() -> None:
    rc = FakeRefreshClient([PatRefreshBadRequest()])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatInternalError):
        await service.resolve("alice")

    assert repo.rows["alice"]["status"] == "active"


async def test_refresh_lock_loser_uses_freshly_cached_token() -> None:
    # Simulate a concurrent refresh already holding the lock and having written a
    # fresh token: the loser must return it WITHOUT calling the refresh client.
    rc = FakeRefreshClient([])  # empty — a call would IndexError
    service, repo, cache, cipher = build_service(refresh_client=rc)

    fresh = sign("alice")
    # expired token in DB (drives resolve into _refresh), fresh valid token in cache
    repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": cipher.encrypt(sign("alice", exp_delta=-10)),
        "status": "active",
    }
    cache.acquire_refresh_lock("alice")  # someone else holds it
    cache.put("alice", cipher.encrypt(fresh))

    # First resolve sees the expired cache entry? No — cache now holds `fresh`
    # (valid), so resolve returns it directly. Drive the lock path explicitly:
    assert await service._refresh("alice", sign("alice", exp_delta=-10)) == fresh
    assert rc.calls == []  # loser never called the refresh service
