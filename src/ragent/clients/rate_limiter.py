"""T3.14 — Fixed-window per-key rate limiter backed by Redis INCR+EXPIRE (B31)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import redis as redis_lib
import structlog

from ragent.utility.env import parse_sentinel_hosts

logger = structlog.get_logger(__name__)

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
        try:
            pipe = self._redis.pipeline()
            pipe.incr(full_key)
            pipe.expire(full_key, window_seconds, nx=True)
            count, _ = pipe.execute()
        except redis_lib.RedisError as exc:
            logger.warning(
                "rate_limiter.redis_unavailable", error_type=type(exc).__name__, error=str(exc)
            )
            return RateLimitResult(allowed=True, remaining=-1, reset_at=None)
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
            master_pw = os.environ.get("REDIS_SENTINEL_MASTER_PASSWORD") or None
            sentinel_pw = os.environ.get("REDIS_SENTINEL_PASSWORD") or None
            sentinel = Sentinel(
                parse_sentinel_hosts(hosts_raw),
                password=master_pw,
                sentinel_kwargs={"password": sentinel_pw} if sentinel_pw else None,
            )
            return cls(redis_client=sentinel.master_for(master))

        url = os.environ.get("REDIS_RATELIMIT_URL", "redis://localhost:6379/1")
        return cls(redis_client=redis_lib.from_url(url))
