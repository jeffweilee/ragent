"""T-PAT.5 — PatCache: TTL round-trip, single-flight lock, fail-soft."""

from __future__ import annotations

import fakeredis
import pytest
import redis as redis_lib

from ragent.clients.pat_cache import PatCache


def _cache(ttl: int = 41400, lock_ttl: int = 10, tombstone_ttl: int = 123) -> PatCache:
    return PatCache(
        fakeredis.FakeStrictRedis(decode_responses=True),
        ttl_seconds=ttl,
        lock_ttl_seconds=lock_ttl,
        tombstone_ttl_seconds=tombstone_ttl,
    )


def test_put_then_get_round_trips() -> None:
    cache = _cache()
    cache.put("alice", "v1.n.c")
    assert cache.get("alice") == "v1.n.c"


def test_get_missing_returns_none() -> None:
    assert _cache().get("ghost") is None


def test_put_sets_ttl() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = PatCache(client, ttl_seconds=123, lock_ttl_seconds=10, tombstone_ttl_seconds=123)
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
    cache = PatCache(client, ttl_seconds=41400, lock_ttl_seconds=45, tombstone_ttl_seconds=123)

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
    return PatCache(
        _RaisingRedis(), ttl_seconds=41400, lock_ttl_seconds=10, tombstone_ttl_seconds=123
    )


def test_get_is_fail_soft(failing_cache: PatCache) -> None:
    assert failing_cache.get("alice") is None


def test_put_evict_are_fail_soft(failing_cache: PatCache) -> None:
    failing_cache.put("alice", "v1.n.c")  # must not raise
    failing_cache.evict("alice")  # must not raise


def test_acquire_lock_fail_soft_returns_none(failing_cache: PatCache) -> None:
    assert failing_cache.acquire_refresh_lock("alice") is None


def test_release_lock_is_fail_soft(failing_cache: PatCache) -> None:
    failing_cache.release_refresh_lock("alice", "tok")  # must not raise


# --- revocation tombstone (T-PAT.26) -------------------------------------


def test_put_is_refused_while_a_tombstone_is_set() -> None:
    cache = _cache()
    cache.mark_revoked("alice")

    cache.put("alice", "v1.n.c")

    assert cache.get("alice") is None


def test_tombstone_is_scoped_to_one_nt() -> None:
    cache = _cache()
    cache.mark_revoked("alice")

    cache.put("bob", "v1.n.c")

    assert cache.get("bob") == "v1.n.c"


def test_put_resumes_once_the_tombstone_expires() -> None:
    # The tombstone only has to outlast in-flight refreshes; a later
    # re-authorization must be able to populate the cache again.
    cache = _cache(tombstone_ttl=1)
    cache.mark_revoked("alice")
    cache._redis.delete("ragent:pat:tomb:alice")  # simulate the TTL lapsing

    cache.put("alice", "v1.n.c")

    assert cache.get("alice") == "v1.n.c"


def test_mark_revoked_is_fail_soft(failing_cache: PatCache) -> None:
    failing_cache.mark_revoked("alice")  # a Redis outage must not raise


def test_tombstone_ttl_is_derived_from_the_refresh_budget() -> None:
    # Derived, not a constant and not a new env var: retuning the refresh budget
    # must not silently shrink the tombstone below the window it covers. Takes
    # the values rather than re-reading env, so the composition root stays the
    # single reader (a second one would carry its own copy of the defaults).
    # 30 * (3 + 1) + 0.5 * (1 + 2 + 4) = 123.5 -> 124
    assert PatCache.tombstone_ttl_for(30.0, 3, 0.5) == 124
    assert PatCache.tombstone_ttl_for(60.0, 3, 0.5) == 244
    # A shrunken budget shrinks the tombstone with it, never independently.
    assert PatCache.tombstone_ttl_for(5.0, 0, 0.5) == 5
