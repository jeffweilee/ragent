"""PatCache — Redis cache for encrypted PATs + per-nt refresh lock (T-PAT).

A cache of the `pat` DB row: `ragent:pat:{nt}` → encrypted PAT, TTL 11.5 h (0.5 h
under the 12 h PAT lifetime). Every operation is **fail-soft**: a `RedisError`
degrades to a cache miss / no-op (logged `pat.redis_unavailable`) so a Redis blip
falls back to the DB rather than 500-ing the request — mirroring `RateLimiter` /
`ChatStreamStore`.

`acquire_refresh_lock` (`SET NX EX`) serialises refresh per nt so concurrent
requests don't stampede the refresh API or clobber each other's token rotation;
a fail-soft `False` on Redis error simply means the caller proceeds unserialised
(still correct, just no herd protection while Redis is down).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import redis as redis_lib
import structlog

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "ragent:pat:"
_LOCK_PREFIX = "ragent:pat:lock:"


class PatCache:
    def __init__(self, redis_client: Any, *, ttl_seconds: int, lock_ttl_seconds: int) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._lock_ttl = lock_ttl_seconds

    def get(self, nt: str) -> str | None:
        try:
            return self._redis.get(f"{_KEY_PREFIX}{nt}")
        except redis_lib.RedisError as exc:
            self._unavailable("get", exc)
            return None

    def put(self, nt: str, pat_cipher: str) -> None:
        try:
            self._redis.set(f"{_KEY_PREFIX}{nt}", pat_cipher, ex=self._ttl)
        except redis_lib.RedisError as exc:
            self._unavailable("put", exc)

    def evict(self, nt: str) -> None:
        try:
            self._redis.delete(f"{_KEY_PREFIX}{nt}")
        except redis_lib.RedisError as exc:
            self._unavailable("evict", exc)

    def acquire_refresh_lock(self, nt: str) -> str | None:
        """Try to take the per-nt refresh lock. Returns a unique owner token on
        success (pass it back to :meth:`release_refresh_lock`), or ``None`` if
        another holder has it / Redis is unavailable.

        The token identifies THIS holder: if the lock's TTL expires mid-refresh
        and another request re-acquires it, our release must not delete the new
        owner's lock (Codex review r3619473859)."""
        token = uuid.uuid4().hex
        try:
            acquired = self._redis.set(f"{_LOCK_PREFIX}{nt}", token, nx=True, ex=self._lock_ttl)
        except redis_lib.RedisError as exc:
            self._unavailable("lock", exc)
            return None
        return token if acquired else None

    def release_refresh_lock(self, nt: str, token: str) -> None:
        """Release the lock only if we still own it (atomic compare-and-delete
        via WATCH/MULTI) — never delete a lock a later holder re-acquired."""
        key = f"{_LOCK_PREFIX}{nt}"
        try:
            with self._redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.get(key) == token:
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
                else:
                    pipe.unwatch()
        except redis_lib.WatchError:
            # Someone changed the key between WATCH and MULTI — not ours to delete.
            pass
        except redis_lib.RedisError as exc:
            self._unavailable("unlock", exc)

    @staticmethod
    def _unavailable(op: str, exc: redis_lib.RedisError) -> None:
        logger.warning("pat.redis_unavailable", op=op, error_type=type(exc).__name__)

    @classmethod
    def from_env(cls) -> PatCache:
        ttl = int(os.environ.get("REDIS_PAT_TTL_SECONDS", "41400"))
        lock_ttl = int(os.environ.get("REDIS_PAT_LOCK_TTL_SECONDS", "45"))
        mode = os.environ.get("REDIS_MODE", "standalone")
        if mode == "sentinel":
            from redis.sentinel import Sentinel

            hosts_raw = os.environ.get("REDIS_SENTINEL_HOSTS", "")
            master = os.environ.get("REDIS_PAT_SENTINEL_MASTER", "pat-master")
            sentinels = [
                (h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1]))
                for h in hosts_raw.split(",")
                if h.strip()
            ]
            client = Sentinel(sentinels).master_for(master, decode_responses=True)
            return cls(client, ttl_seconds=ttl, lock_ttl_seconds=lock_ttl)

        url = os.environ.get("REDIS_PAT_URL", "redis://localhost:6379/3")
        return cls(
            redis_lib.from_url(url, decode_responses=True),
            ttl_seconds=ttl,
            lock_ttl_seconds=lock_ttl,
        )
