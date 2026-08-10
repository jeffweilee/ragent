"""T-RG.3 — the three Redis surfaces degrade to their fail-soft default.

Two properties per surface:

1. **Correct default.** A Redis failure yields the documented degraded answer
   (rate limiting allows, the PAT cache misses, the stream buffer reports
   unavailable) rather than an exception reaching the request.
2. **Zero I/O once open.** After the breaker trips, further calls must not
   touch the socket at all — that is what stops a dead Redis from taxing every
   subsequent request with the connect timeout.
"""

from __future__ import annotations

import fakeredis
import pytest
import redis as redis_lib

from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.pat_cache import PatCache
from ragent.clients.rate_limiter import RateLimiter
from ragent.clients.redis_guard import UNAVAILABLE, RedisCircuit


class _DeadRedis:
    """Every command raises, and every attempt is counted."""

    def __init__(self) -> None:
        self.attempts = 0

    def __getattr__(self, _name: str):
        def _fail(*_args, **_kwargs):
            self.attempts += 1
            raise redis_lib.ConnectionError("connection refused")

        return _fail


def _circuit() -> RedisCircuit:
    return RedisCircuit("test", failure_threshold=1, cooldown_seconds=300.0)


def _pat_cache(client) -> PatCache:
    return PatCache(
        client,
        ttl_seconds=10,
        lock_ttl_seconds=10,
        tombstone_ttl_seconds=10,
        circuit=_circuit(),
    )


def _stream_store(client) -> ChatStreamStore:
    return ChatStreamStore(client, circuit=_circuit())


# --- RateLimiter -------------------------------------------------------


def test_rate_limiter_fails_open_and_then_stops_calling_redis() -> None:
    dead = _DeadRedis()
    limiter = RateLimiter(dead, circuit=_circuit())

    first = limiter.check("u", limit=1, window_seconds=60)
    assert first.allowed is True  # fail-open
    assert dead.attempts == 1

    for _ in range(5):
        assert limiter.check("u", limit=1, window_seconds=60).allowed is True
    assert dead.attempts == 1  # circuit open — never touched again


# --- PatCache ----------------------------------------------------------


def test_pat_cache_get_misses_and_then_stops_calling_redis() -> None:
    dead = _DeadRedis()
    cache = _pat_cache(dead)

    assert cache.get("alice") is None
    assert dead.attempts == 1

    for _ in range(5):
        assert cache.get("alice") is None
    assert dead.attempts == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.put("alice", "v1.n.c"),
        lambda c: c.evict("alice"),
        lambda c: c.mark_revoked("alice"),
        lambda c: c.release_refresh_lock("alice", "tok"),
    ],
    ids=["put", "evict", "mark_revoked", "release_refresh_lock"],
)
def test_pat_cache_writes_never_raise_on_a_dead_redis(call) -> None:
    call(_pat_cache(_DeadRedis()))  # must not raise


def test_acquire_refresh_lock_reports_unavailable_distinctly_from_held() -> None:
    """The caller must tell "someone else holds it" from "Redis is gone".

    Polling is the right move for the former and pure waste for the latter —
    nobody can ever take a lock that cannot be written.
    """
    held = _pat_cache(fakeredis.FakeStrictRedis(decode_responses=True))
    token = held.acquire_refresh_lock("alice")
    assert isinstance(token, str)
    assert held.acquire_refresh_lock("alice") is None  # held by another

    assert _pat_cache(_DeadRedis()).acquire_refresh_lock("alice") is UNAVAILABLE


def test_put_still_honours_the_revocation_tombstone() -> None:
    """The breaker must not weaken revoke()'s only choke point."""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = _pat_cache(client)
    cache.mark_revoked("alice")
    cache.put("alice", "v1.n.c")
    assert cache.get("alice") is None


# --- ChatStreamStore ---------------------------------------------------


def test_try_start_reports_unavailable_so_v3_takes_the_legacy_path() -> None:
    assert _stream_store(_DeadRedis()).try_start("k") is None


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.append("k", "frame"),
        lambda s: s.mark_done("k"),
        lambda s: s.set_current("u", "t", "r"),
        lambda s: s.stash_user_input("k", "hi"),
        lambda s: s.mark_unread("u", "t"),
    ],
    ids=["append", "mark_done", "set_current", "stash_user_input", "mark_unread"],
)
def test_stream_writes_never_raise_on_a_dead_redis(call) -> None:
    """append/mark_done run in the fire-and-forget producer thread.

    An escaping error there aborts generation mid-run and leaves the consumer
    to time out with a truncated stream and no error frame.
    """
    call(_stream_store(_DeadRedis()))


def test_stream_reads_return_safe_defaults_on_a_dead_redis() -> None:
    store = _stream_store(_DeadRedis())
    assert store.read_after("k", None) == []
    assert store.is_done("k") is False
    assert store.is_resumable("k") is False
    assert store.get_current("u", "t") is None
    assert store.has_unread("u", "t") is False
    assert store.status_many("u", ["t1", "t2"]) == {
        "t1": {"running": False, "hasNewReply": False},
        "t2": {"running": False, "hasNewReply": False},
    }


def test_each_client_owns_its_own_circuit() -> None:
    """Sentinel deployments put these on four distinct masters — a dead
    stream-master must not stop the PAT cache from being consulted."""
    dead = _DeadRedis()
    alive = fakeredis.FakeStrictRedis(decode_responses=True)
    stream = ChatStreamStore(dead)
    cache = PatCache(alive, ttl_seconds=10, lock_ttl_seconds=10, tombstone_ttl_seconds=10)

    for _ in range(5):
        stream.set_current("u", "t", "r")

    cache.put("alice", "v1.n.c")
    assert cache.get("alice") == "v1.n.c"
