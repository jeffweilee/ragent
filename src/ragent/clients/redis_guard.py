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
import threading
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
    """Guards one Redis client.

    Deliberately mixed locking. The failure counter is a plain int mutated
    without a lock: the worst case is an off-by-a-few count, which shifts *when*
    the circuit trips by a call or two and cannot produce a wrong state, so a
    lock there would contend the hot path to protect a number whose exact value
    carries no meaning. The half-open reservation IS locked — admitting every
    concurrent caller instead of one costs a real socket timeout each, every
    cooldown, which is the tax this class exists to remove.
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
        # Guards the half-open reservation only. The failure counter stays
        # lock-free: an off-by-a-few count merely shifts *when* the circuit trips
        # by a call or two and cannot produce a wrong state, so it is not worth
        # contending the hot path for. Admitting N concurrent probes instead of
        # one is not in that category — it has a real, repeating latency cost.
        self._lock = threading.Lock()
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
        """True when closed, or for the ONE caller that reserves the probe.

        The reservation matters: `ChatStreamStore` is shared by request handlers
        and the producer pool, so an open circuit has many concurrent callers.
        A bare ``now - opened_at >= cooldown`` check lets all of them through in
        the window between the check and the first result, and each pays the
        full socket timeout — reinstating exactly the per-request tax the
        breaker exists to remove.

        Reserving is just re-stamping the cooldown: the winner probes while
        everyone else sees a fresh window and short-circuits. `_record_success` /
        `_record_failure` then set the real outcome.
        """
        if self._opened_at is None:
            return True  # closed — the hot path stays lock-free
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at < self._cooldown:
                return False
            self._opened_at = self._clock()
            return True

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


def connection_timeouts(*, blocking: bool = False) -> dict[str, float]:
    """The bounded socket budget every ragent Redis client shares.

    ``socket_connect_timeout`` is the one that matters for a blackholed
    endpoint; it is tight because establishing a connection to a healthy Redis
    is a single RTT. ``socket_timeout`` covers command execution and is looser
    — some ops legitimately do work (``XRANGE`` over a full stream buffer,
    pipelined batches in ``status_many``) and a false trip there would disable a
    cache that is fine.

    ``blocking=True`` omits ``socket_timeout`` entirely, and callers that park in
    a blocking command MUST use it. `ListQueueBroker.listen()` calls
    ``brpop(queue_name)`` with no timeout argument — it waits indefinitely for a
    task — so a read timeout tears that socket down mid-wait and raises
    ``redis.TimeoutError``. That is not a ``ConnectionError`` subclass, so
    listen()'s ``except ConnectionError`` does not catch it: the generator dies
    and the worker stops consuming, silently, leaving documents at UPLOADED.
    The connect timeout is still applied — it only bounds establishing the
    connection, never a blocking read, so the blackhole case stays covered.
    """
    timeouts = {
        "socket_connect_timeout": float(os.environ.get("REDIS_CONNECT_TIMEOUT_SECONDS", "0.25")),
    }
    if not blocking:
        timeouts["socket_timeout"] = float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "1"))
    return timeouts


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
