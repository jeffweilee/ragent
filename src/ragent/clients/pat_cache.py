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

import math
import os
import uuid
from typing import Any

import redis as redis_lib
import structlog

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "ragent:pat:"
_LOCK_PREFIX = "ragent:pat:lock:"
_TOMB_PREFIX = "ragent:pat:tomb:"


class PatCache:
    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int,
        lock_ttl_seconds: int,
        tombstone_ttl_seconds: int,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._lock_ttl = lock_ttl_seconds
        self._tombstone_ttl = tombstone_ttl_seconds

    def get(self, nt: str) -> str | None:
        try:
            return self._redis.get(f"{_KEY_PREFIX}{nt}")
        except redis_lib.RedisError as exc:
            self._unavailable("get", exc)
            return None

    def put(self, nt: str, pat_cipher: str) -> None:
        """Cache ``pat_cipher`` — UNLESS the nt carries a revocation tombstone.

        The guard lives here rather than at each call site because this is the
        single choke point every repopulation path goes through (``resolve``'s
        re-fill and ``_do_refresh``'s rotation), so a path added later is covered
        by construction. Without it, a refresh that rotated *before* a revoke can
        still ``put`` *after* the eviction — `_do_refresh` awaits between its DB
        write and this call, so a whole revoke fits in that gap on one event loop
        — and because cache hits never consult the DB, the revoked credential
        would keep riding every brain call for the full TTL.

        Written under WATCH/MULTI (the idiom `release_refresh_lock` uses) so the
        check and the write are atomic: a tombstone landing between them aborts
        the write rather than being overwritten by it.
        """
        key = f"{_KEY_PREFIX}{nt}"
        tomb = f"{_TOMB_PREFIX}{nt}"
        try:
            with self._redis.pipeline() as pipe:
                pipe.watch(tomb)
                if pipe.exists(tomb):
                    pipe.unwatch()
                    logger.info("pat.cache.put_refused_revoked", user_id=nt)
                    return
                pipe.multi()
                pipe.set(key, pat_cipher, ex=self._ttl)
                pipe.execute()
        except redis_lib.WatchError:
            # A revoke touched the tombstone mid-transaction — do not publish.
            logger.info("pat.cache.put_refused_revoked", user_id=nt)
        except redis_lib.RedisError as exc:
            self._unavailable("put", exc)

    def mark_revoked(self, nt: str) -> None:
        """Block cache repopulation for this nt while in-flight work drains.

        TTL is sized to outlast the longest possible in-flight refresh, so any
        rotation that started before the revoke has finished (and been refused)
        before the tombstone lapses.
        """
        try:
            self._redis.set(f"{_TOMB_PREFIX}{nt}", "1", ex=self._tombstone_ttl)
        except redis_lib.RedisError as exc:
            self._unavailable("tombstone", exc)

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

    @staticmethod
    def tombstone_ttl_for(timeout: float, max_retries: int, backoff_base: float) -> int:
        """Worst-case in-flight refresh, DERIVED from the refresh budget.

        A revoke must keep refusing cache writes until every refresh that started
        before it has finished. That bound is the refresh timeout across the
        initial call plus each retry, plus the summed exponential backoff between
        them (~123 s at the 30/3/0.5 defaults).

        Takes the values rather than reading the env itself: the composition root
        already resolves this exact trio for `PatRefreshClient` and `PatService`,
        and a second reader would carry its own copy of the defaults — so
        retuning the refresh budget in one place would silently leave the
        tombstone sized for the old one, which is the drift deriving it was meant
        to rule out.
        """
        backoff_total = backoff_base * (2**max_retries - 1)  # base * (2^0 + … + 2^(n-1))
        return math.ceil(timeout * (max_retries + 1) + backoff_total)

    @classmethod
    def from_env(cls, *, tombstone_ttl_seconds: int) -> PatCache:
        ttl = int(os.environ.get("REDIS_PAT_TTL_SECONDS", "41400"))
        lock_ttl = int(os.environ.get("REDIS_PAT_LOCK_TTL_SECONDS", "45"))
        tombstone_ttl = tombstone_ttl_seconds
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
            return cls(
                client,
                ttl_seconds=ttl,
                lock_ttl_seconds=lock_ttl,
                tombstone_ttl_seconds=tombstone_ttl,
            )

        url = os.environ.get("REDIS_PAT_URL", "redis://localhost:6379/3")
        return cls(
            redis_lib.from_url(url, decode_responses=True),
            ttl_seconds=ttl,
            lock_ttl_seconds=lock_ttl,
            tombstone_ttl_seconds=tombstone_ttl,
        )
