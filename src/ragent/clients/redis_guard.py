"""T-RG.1 — Circuit breaker guarding a single Redis client.

Every ragent Redis surface is a **weak dependency**: rate limiting fails open,
the PAT cache falls through to MariaDB, and the chat-stream buffer degrades to a
connection-bound SSE stream. Per-call `except RedisError` already delivered that
contract for a Redis that *fails*, but not for one that *hangs* — the expensive
case is a blackholed endpoint (mid-failover master, dropped packets), where every
call pays the full socket timeout before the fail-soft branch is even reached.

This breaker bounds that cost in time rather than per call. After
``failure_threshold`` consecutive connectivity failures it opens, and every
subsequent call returns :data:`UNAVAILABLE` without touching the socket — the
degraded path costs nothing. One probe is allowed through once
``cooldown_seconds`` has passed; it closes the circuit on success and restarts
the cooldown on failure, so a still-dead Redis is retried on a fixed schedule
instead of on every request.

**Granularity is per client, not per process.** In sentinel deployments the four
Redis surfaces are four distinct masters (`ragent-broker`, `ratelimit-master`,
`stream-master`, `pat-master`), so a dead stream master must not stop the PAT
cache from being consulted.

``WatchError`` is deliberately re-raised rather than counted: it subclasses
``RedisError`` but signals an optimistic-lock conflict from a perfectly healthy
server, and `PatCache.put` depends on observing it. Counting it would let
ordinary write contention disable the cache.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

import redis as redis_lib
import structlog

from ragent.bootstrap.metrics import redis_circuit_state, redis_circuit_trips_total
from ragent.utility.env import parse_sentinel_hosts

logger = structlog.get_logger(__name__)

T = TypeVar("T")

_CLOSED = "closed"
_OPEN = "open"


class RedisUnavailable:
    """Sentinel returned when a call did not reach Redis.

    Falsy on purpose: call sites whose fail-soft default is already a falsy
    value (``None`` / ``False`` / ``0``) can keep using a plain truth check,
    while the two sites that must tell "Redis is down" apart from a legitimate
    negative answer (`PatCache.acquire_refresh_lock`, `ChatStreamStore.try_start`)
    compare against the singleton identity.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "<redis unavailable>"


UNAVAILABLE = RedisUnavailable()


class RedisCircuit:
    """Guards one Redis client. Not thread-safe by design — see note below.

    The counters are plain ints mutated without a lock. Under concurrent access
    the worst case is an off-by-a-few failure count, which shifts *when* the
    circuit trips by at most a couple of calls; it cannot produce a wrong state.
    A lock here would put contention on the hot path to protect a number whose
    exact value carries no meaning.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._threshold = max(1, failure_threshold)
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        redis_circuit_state.labels(client=name).set(0)

    @property
    def state(self) -> str:
        return _OPEN if self._opened_at is not None else _CLOSED

    def call(self, op: str, fn: Callable[[], T]) -> T | RedisUnavailable:
        """Run ``fn`` under the breaker.

        Returns ``fn``'s result, or :data:`UNAVAILABLE` if the circuit is open
        or the call failed on connectivity. Non-``RedisError`` exceptions (our
        own bugs) and ``WatchError`` (a healthy-server outcome) propagate.
        """
        if not self._may_call():
            return UNAVAILABLE
        try:
            result = fn()
        except redis_lib.WatchError:
            # Healthy server, contended key — not a connectivity signal.
            raise
        except redis_lib.RedisError as exc:
            self._record_failure(op, exc)
            return UNAVAILABLE
        self._record_success()
        return result

    def _may_call(self) -> bool:
        """True when closed, or when an open circuit is due for its probe."""
        if self._opened_at is None:
            return True
        return self._clock() - self._opened_at >= self._cooldown

    def _record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("redis.circuit_closed", client=self._name)
            redis_circuit_state.labels(client=self._name).set(0)
        self._failures = 0
        self._opened_at = None

    def _record_failure(self, op: str, exc: redis_lib.RedisError) -> None:
        was_open = self._opened_at is not None
        self._failures += 1
        if was_open or self._failures >= self._threshold:
            # Re-stamp on a failed probe too, so a still-dead Redis is retried
            # once per cooldown rather than on every subsequent request.
            self._opened_at = self._clock()
            if not was_open:
                logger.warning(
                    "redis.circuit_opened",
                    client=self._name,
                    op=op,
                    failures=self._failures,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                redis_circuit_trips_total.labels(client=self._name).inc()
                redis_circuit_state.labels(client=self._name).set(1)
            return
        logger.warning(
            "redis.unavailable",
            client=self._name,
            op=op,
            error_type=type(exc).__name__,
            error=str(exc),
        )


def unwrap(value: T | RedisUnavailable, default: Any) -> Any:
    """Collapse a circuit result to its fail-soft default."""
    return default if isinstance(value, RedisUnavailable) else value


def connection_timeouts() -> dict[str, float]:
    """The bounded socket budget every ragent Redis client shares.

    ``socket_connect_timeout`` is the one that matters for a blackholed
    endpoint; it is tight because establishing a connection to a healthy Redis
    is a single RTT. ``socket_timeout`` covers command execution and is looser
    — some ops legitimately do work (``XRANGE`` over a full stream buffer,
    pipelined batches in ``status_many``) and a false trip there would disable a
    cache that is fine.
    """
    return {
        "socket_connect_timeout": float(os.environ.get("REDIS_CONNECT_TIMEOUT_SECONDS", "0.25")),
        "socket_timeout": float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "1")),
    }


def circuit_from_env(name: str) -> RedisCircuit:
    """The breaker for one surface, tuned by the shared operator knobs."""
    return RedisCircuit(
        name,
        failure_threshold=int(os.environ.get("REDIS_CIRCUIT_FAILURE_THRESHOLD", "3")),
        cooldown_seconds=float(os.environ.get("REDIS_CIRCUIT_COOLDOWN_SECONDS", "5")),
    )


def build_redis_client(
    *,
    master_env: str,
    master_default: str,
    url_env: str,
    url_default: str,
    decode_responses: bool = False,
) -> Any:
    """Build the Redis client for one ragent surface (standalone or sentinel).

    Centralised because the sentinel branch is easy to get subtly wrong: the
    timeout budget has to reach the master connections **and** the sentinel
    discovery connections. Discovery is the hang that actually bites during a
    failover — that is precisely when the old master stops answering — and an
    unbounded ``sentinel_kwargs`` leaves it uncapped no matter what the master
    connections are configured with.
    """
    timeouts = connection_timeouts()
    if os.environ.get("REDIS_MODE", "standalone") != "sentinel":
        url = os.environ.get(url_env, url_default)
        return redis_lib.from_url(url, decode_responses=decode_responses, **timeouts)

    from redis.sentinel import Sentinel

    sentinel_pw = os.environ.get("REDIS_SENTINEL_PASSWORD") or None
    sentinel = Sentinel(
        parse_sentinel_hosts(os.environ.get("REDIS_SENTINEL_HOSTS", "")),
        password=os.environ.get("REDIS_SENTINEL_MASTER_PASSWORD") or None,
        sentinel_kwargs={**timeouts, **({"password": sentinel_pw} if sentinel_pw else {})},
        **timeouts,
    )
    return sentinel.master_for(
        os.environ.get(master_env, master_default), decode_responses=decode_responses
    )
