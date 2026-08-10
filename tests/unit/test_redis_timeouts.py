"""T-RG.2 — every Redis client must carry a bounded socket budget.

redis-py defaults ``socket_timeout`` and ``socket_connect_timeout`` to ``None``.
Against a Redis that *refuses* connections that is harmless (ECONNREFUSED comes
back in microseconds), but against one that silently drops packets — a master
mid-failover, a moved security group — an unbounded connect blocks until the
kernel gives up (``tcp_syn_retries=6`` ≈ 127 s). These clients are called
directly from the event loop, so that stalls every other in-flight request on
the worker.

Each client is asserted in BOTH topologies. In sentinel mode the budget must
also reach ``sentinel_kwargs``: master discovery runs over its own connections,
and an unbounded discovery hangs in exactly the failover this is meant to survive.
"""

from __future__ import annotations

import pytest

from ragent.clients.chat_stream_store import ChatStreamStore
from ragent.clients.pat_cache import PatCache
from ragent.clients.rate_limiter import RateLimiter

_SENTINEL_ENV = {
    "REDIS_MODE": "sentinel",
    "REDIS_SENTINEL_HOSTS": "sentinel-a:26379,sentinel-b:26379",
}


def _pool_kwargs(client) -> dict:
    return client.connection_pool.connection_kwargs


def _assert_bounded(kwargs: dict, *, connect: float, op: float) -> None:
    assert kwargs.get("socket_connect_timeout") == connect
    assert kwargs.get("socket_timeout") == op


# --- standalone --------------------------------------------------------


def test_rate_limiter_standalone_has_bounded_timeouts(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_MODE", raising=False)
    _assert_bounded(_pool_kwargs(RateLimiter.from_env()._redis), connect=0.25, op=1.0)


def test_pat_cache_standalone_has_bounded_timeouts(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_MODE", raising=False)
    cache = PatCache.from_env(tombstone_ttl_seconds=123)
    _assert_bounded(_pool_kwargs(cache._redis), connect=0.25, op=1.0)


def test_chat_stream_store_standalone_has_bounded_timeouts(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_MODE", raising=False)
    _assert_bounded(_pool_kwargs(ChatStreamStore.from_env()._redis), connect=0.25, op=1.0)


# --- env overrides -----------------------------------------------------


def test_timeouts_are_operator_tunable(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_MODE", raising=False)
    monkeypatch.setenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "2.5")
    _assert_bounded(_pool_kwargs(RateLimiter.from_env()._redis), connect=0.05, op=2.5)


# --- sentinel ----------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "master_env", "master_default"),
    [
        (lambda: RateLimiter.from_env()._redis, "REDIS_RATELIMIT_SENTINEL_MASTER", "ratelimit"),
        (
            lambda: PatCache.from_env(tombstone_ttl_seconds=123)._redis,
            "REDIS_PAT_SENTINEL_MASTER",
            "pat",
        ),
        (lambda: ChatStreamStore.from_env()._redis, "REDIS_STREAM_SENTINEL_MASTER", "stream"),
    ],
)
def test_sentinel_master_connections_have_bounded_timeouts(
    monkeypatch, build, master_env, master_default
) -> None:
    for key, value in _SENTINEL_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(master_env, f"{master_default}-master")
    _assert_bounded(_pool_kwargs(build()), connect=0.25, op=1.0)


@pytest.mark.parametrize(
    "build",
    [
        lambda: RateLimiter.from_env()._redis,
        lambda: PatCache.from_env(tombstone_ttl_seconds=123)._redis,
        lambda: ChatStreamStore.from_env()._redis,
    ],
)
def test_sentinel_discovery_connections_have_bounded_timeouts(monkeypatch, build) -> None:
    """Discovery is the hang that matters during a failover — bound it too."""
    for key, value in _SENTINEL_ENV.items():
        monkeypatch.setenv(key, value)
    sentinel_manager = build().connection_pool.sentinel_manager
    for sentinel in sentinel_manager.sentinels:
        _assert_bounded(_pool_kwargs(sentinel), connect=0.25, op=1.0)
