"""T-PAT.8 — PatService.resolve: redis→DB→refresh→invalidate branches."""

from __future__ import annotations

import pytest

from ragent.services.pat_service import PatReauthRequired
from tests.unit.pat_fakes import FakeRefreshClient, build_service, sign


async def test_redis_hit_valid_returns_without_refresh() -> None:
    service, _, cache, cipher = build_service(refresh_client=FakeRefreshClient([]))
    pat = sign("alice")
    cache.put("alice", cipher.encrypt(pat))

    assert await service.resolve("alice") == pat


async def test_redis_miss_db_active_valid_repopulates_cache() -> None:
    service, repo, cache, cipher = build_service()
    pat = sign("alice")
    repo.rows["alice"] = {"user_id": "alice", "pat_cipher": cipher.encrypt(pat), "status": "active"}

    assert await service.resolve("alice") == pat
    # redis was repopulated from the DB row.
    assert cache.get("alice") is not None


async def test_db_invalid_raises_reauth() -> None:
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": cipher.encrypt(sign()),
        "status": "invalid",
    }

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")


async def test_db_absent_raises_reauth() -> None:
    service, _, _, _ = build_service()
    with pytest.raises(PatReauthRequired):
        await service.resolve("ghost")


async def test_expired_cache_triggers_refresh() -> None:
    new_pat = sign("alice")
    service, repo, cache, cipher = build_service(refresh_client=FakeRefreshClient([new_pat]))
    cache.put("alice", cipher.encrypt(sign("alice", exp_delta=-10)))  # expired
    repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": cipher.encrypt(sign("alice", exp_delta=-10)),
        "status": "active",
    }

    assert await service.resolve("alice") == new_pat
    assert repo.rows["alice"]["status"] == "active"


async def test_best_effort_swallows_reauth() -> None:
    service, _, _, _ = build_service()
    assert await service.resolve_best_effort("ghost") is None


async def test_best_effort_empty_nt_returns_none() -> None:
    service, _, _, _ = build_service()
    assert await service.resolve_best_effort("") is None
