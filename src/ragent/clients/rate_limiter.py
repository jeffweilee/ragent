"""T3.14 — Fixed-window per-key rate limiter backed by Redis INCR+EXPIRE (B31)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

_KEY_PREFIX = "ratelimit:"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_at: float | None


class RateLimiter:
    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        full_key = f"{_KEY_PREFIX}{key}"
        # Pipeline batches INCR + EXPIRE NX in one round-trip.
        # EXPIRE NX sets TTL only if the key has no expiry, preventing immortal keys
        # if a process crashes after INCR but before a separate EXPIRE call.
        pipe = self._redis.pipeline()
        pipe.incr(full_key)
        pipe.expire(full_key, window_seconds, nx=True)
        count, _ = pipe.execute()
        if count > limit:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=time.time() + window_seconds,
            )
        return RateLimitResult(allowed=True, remaining=limit - count, reset_at=None)

    @classmethod
    def from_env(cls) -> RateLimiter:
        mode = os.environ.get("REDIS_MODE", "standalone")
        if mode == "sentinel":
            from redis.sentinel import Sentinel

            hosts_raw = os.environ.get("REDIS_SENTINEL_HOSTS", "")
            master = os.environ.get("REDIS_RATELIMIT_SENTINEL_MASTER", "ratelimit-master")
            sentinels = [
                (h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1]))
                for h in hosts_raw.split(",")
                if h.strip()
            ]
            sentinel = Sentinel(sentinels)
            return cls(redis_client=sentinel.master_for(master))
        import redis

        url = os.environ.get("REDIS_RATELIMIT_URL", "redis://localhost:6379/1")
        return cls(redis_client=redis.from_url(url))
