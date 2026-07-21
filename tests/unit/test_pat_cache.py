"""T-PAT.5 — PatCache: TTL round-trip, single-flight lock, fail-soft."""

from __future__ import annotations

import fakeredis
import pytest
import redis as redis_lib

from ragent.clients.pat_cache import PatCache


def _cache(ttl: int = 41400, lock_ttl: int = 10) -> PatCache:
    return PatCache(
        fakeredis.FakeStrictRedis(decode_responses=True),
        ttl_seconds=ttl,
        lock_ttl_seconds=lock_ttl,
    )


def test_put_then_get_round_trips() -> None:
    cache = _cache()
    cache.put("alice", "v1.n.c")
    assert cache.get("alice") == "v1.n.c"


def test_get_missing_returns_none() -> None:
    assert _cache().get("ghost") is None


def test_put_sets_ttl() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = PatCache(client, ttl_seconds=123, lock_ttl_seconds=10)
    cache.put("alice", "v1.n.c")
    assert 0 < client.ttl("ragent:pat:alice") <= 123


def test_evict_removes_the_entry() -> None:
    cache = _cache()
    cache.put("alice", "v1.n.c")
    cache.evict("alice")
    assert cache.get("alice") is None


def test_refresh_lock_is_single_flight() -> None:
    cache = _cache()
    token = cache.acquire_refresh_lock("alice")
    assert token  # a unique owner token
    assert cache.acquire_refresh_lock("alice") is None  # already held
    cache.release_refresh_lock("alice", token)
    assert cache.acquire_refresh_lock("alice")  # released → reacquirable


def test_release_only_deletes_own_lock() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = PatCache(client, ttl_seconds=41400, lock_ttl_seconds=45)

    stale_token = cache.acquire_refresh_lock("alice")
    assert stale_token
    # Simulate the lock TTL expiring and a *new* holder re-acquiring it.
    client.set("ragent:pat:lock:alice", "new-owner-token")

    cache.release_refresh_lock("alice", stale_token)  # must NOT delete the new owner's lock

    assert client.get("ragent:pat:lock:alice") == "new-owner-token"


class _RaisingRedis:
    def get(self, *a, **k):
        raise redis_lib.RedisError("down")

    def set(self, *a, **k):
        raise redis_lib.RedisError("down")

    def delete(self, *a, **k):
        raise redis_lib.RedisError("down")

    def pipeline(self, *a, **k):
        raise redis_lib.RedisError("down")


@pytest.fixture
def failing_cache() -> PatCache:
    return PatCache(_RaisingRedis(), ttl_seconds=41400, lock_ttl_seconds=10)


def test_get_is_fail_soft(failing_cache: PatCache) -> None:
    assert failing_cache.get("alice") is None


def test_put_evict_are_fail_soft(failing_cache: PatCache) -> None:
    failing_cache.put("alice", "v1.n.c")  # must not raise
    failing_cache.evict("alice")  # must not raise


def test_acquire_lock_fail_soft_returns_none(failing_cache: PatCache) -> None:
    assert failing_cache.acquire_refresh_lock("alice") is None


def test_release_lock_is_fail_soft(failing_cache: PatCache) -> None:
    failing_cache.release_refresh_lock("alice", "tok")  # must not raise
