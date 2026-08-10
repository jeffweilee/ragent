"""T3.14 — Fixed-window per-key rate limiter backed by Redis INCR+EXPIRE (B31)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ragent.clients.redis_guard import (
    RedisCircuit,
    RedisUnavailable,
    build_redis_client,
    circuit_from_env,
)

_KEY_PREFIX = "ratelimit:"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_at: float | None


class RateLimiter:
    def __init__(self, redis_client: Any, *, circuit: RedisCircuit | None = None) -> None:
        self._redis = redis_client
        self._circuit = circuit or circuit_from_env("ratelimit")

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        full_key = f"{_KEY_PREFIX}{key}"

        # Pipeline batches INCR + EXPIRE NX in one round-trip.
        # EXPIRE NX sets TTL only if the key has no expiry, preventing immortal keys
        # if a process crashes after INCR but before a separate EXPIRE call.
        def _incr() -> int:
            pipe = self._redis.pipeline()
            pipe.incr(full_key)
            pipe.expire(full_key, window_seconds, nx=True)
            count, _ = pipe.execute()
            return count

        result = self._circuit.call("check", _incr)
        if isinstance(result, RedisUnavailable):
            # Fail OPEN: an outage in the limiter must never deny real traffic.
            return RateLimitResult(allowed=True, remaining=-1, reset_at=None)
        count = result
        if count > limit:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=time.time() + window_seconds,
            )
        return RateLimitResult(allowed=True, remaining=limit - count, reset_at=None)

    @classmethod
    def from_env(cls) -> RateLimiter:
        return cls(
            redis_client=build_redis_client(
                master_env="REDIS_RATELIMIT_SENTINEL_MASTER",
                master_default="ratelimit-master",
                url_env="REDIS_RATELIMIT_URL",
                url_default="redis://localhost:6379/1",
            )
        )
