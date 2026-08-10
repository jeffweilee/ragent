"""PatCache — Redis cache for encrypted PATs + per-nt refresh lock (T-PAT).

A cache of the `pat` DB row: `ragent:pat:{nt}` → encrypted PAT, TTL 11.5 h (0.5 h
under the 12 h PAT lifetime). Every operation is **fail-soft**: a connectivity
failure degrades to a cache miss / no-op so a Redis blip falls back to the DB
rather than 500-ing the request — mirroring `RateLimiter` / `ChatStreamStore`.

Fail-soft is delegated to a per-client `RedisCircuit` (T-RG.1) rather than a
per-method `except RedisError`. This surface is the hottest of the three —
`/brainagent/v1` is a catch-all proxy, so *every* upstream call resolves a PAT —
which makes it the one where paying a socket timeout per call hurts most. Once
the circuit opens, calls short-circuit to the miss path with zero I/O.

`acquire_refresh_lock` (`SET NX EX`) serialises refresh per nt so concurrent
requests don't stampede the refresh API or clobber each other's token rotation.
It reports an unreachable Redis as :data:`UNAVAILABLE`, distinct from the `None`
that means "another holder has it": the caller polls for the latter and must not
for the former, because a lock nobody can write is a lock nobody will release.
"""

from __future__ import annotations

import contextlib
import math
import os
import uuid
from typing import Any

import redis as redis_lib
import structlog

from ragent.clients.redis_guard import (
    UNAVAILABLE,
    RedisCircuit,
    RedisUnavailable,
    build_redis_client,
    circuit_from_env,
    unwrap,
)

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
        circuit: RedisCircuit | None = None,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._lock_ttl = lock_ttl_seconds
        self._tombstone_ttl = tombstone_ttl_seconds
        self._circuit = circuit or circuit_from_env("pat")

    def get(self, nt: str) -> str | None:
        return unwrap(
            self._circuit.call("get", lambda: self._redis.get(f"{_KEY_PREFIX}{nt}")), None
        )

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

        def _write() -> bool:
            with self._redis.pipeline() as pipe:
                pipe.watch(tomb)
                if pipe.exists(tomb):
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.set(key, pat_cipher, ex=self._ttl)
                pipe.execute()
                return True

        try:
            wrote = self._circuit.call("put", _write)
        except redis_lib.WatchError:
            # A revoke touched the tombstone mid-transaction — do not publish.
            # Re-raised by the circuit on purpose: a contended key means Redis is
            # healthy, so it must not count toward a trip.
            logger.info("pat.cache.put_refused_revoked", user_id=nt)
            return
        # `is False` — not `not wrote` — because an unavailable Redis is falsy too
        # and that is an outage, not a refusal.
        if wrote is False:
            logger.info("pat.cache.put_refused_revoked", user_id=nt)

    def mark_revoked(self, nt: str) -> None:
        """Block cache repopulation for this nt while in-flight work drains.

        TTL is sized to outlast the longest possible in-flight refresh, so any
        rotation that started before the revoke has finished (and been refused)
        before the tombstone lapses.
        """
        self._circuit.call(
            "tombstone", lambda: self._redis.set(f"{_TOMB_PREFIX}{nt}", "1", ex=self._tombstone_ttl)
        )

    def evict(self, nt: str) -> None:
        self._circuit.call("evict", lambda: self._redis.delete(f"{_KEY_PREFIX}{nt}"))

    def acquire_refresh_lock(self, nt: str) -> str | RedisUnavailable | None:
        """Try to take the per-nt refresh lock.

        Three outcomes, and the caller needs all three:

        - a unique owner token → we hold it (pass it back to
          :meth:`release_refresh_lock`);
        - ``None`` → another holder has it, so polling is worthwhile;
        - :data:`UNAVAILABLE` → Redis is unreachable, so polling is pure waste —
          nobody can take a lock that cannot be written, and the poll loop would
          burn its full sleep budget on every request.

        The token identifies THIS holder: if the lock's TTL expires mid-refresh
        and another request re-acquires it, our release must not delete the new
        owner's lock (Codex review r3619473859)."""
        token = uuid.uuid4().hex
        acquired = self._circuit.call(
            "lock",
            lambda: self._redis.set(f"{_LOCK_PREFIX}{nt}", token, nx=True, ex=self._lock_ttl),
        )
        if isinstance(acquired, RedisUnavailable):
            return UNAVAILABLE
        return token if acquired else None

    def release_refresh_lock(self, nt: str, token: str) -> None:
        """Release the lock only if we still own it (atomic compare-and-delete
        via WATCH/MULTI) — never delete a lock a later holder re-acquired."""
        key = f"{_LOCK_PREFIX}{nt}"

        def _release() -> None:
            with self._redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.get(key) == token:
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
                else:
                    pipe.unwatch()

        # Someone changed the key between WATCH and MULTI — not ours to delete.
        with contextlib.suppress(redis_lib.WatchError):
            self._circuit.call("unlock", _release)

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
        return cls(
            build_redis_client(
                master_env="REDIS_PAT_SENTINEL_MASTER",
                master_default="pat-master",
                url_env="REDIS_PAT_URL",
                url_default="redis://localhost:6379/3",
                decode_responses=True,
            ),
            ttl_seconds=int(os.environ.get("REDIS_PAT_TTL_SECONDS", "41400")),
            lock_ttl_seconds=int(os.environ.get("REDIS_PAT_LOCK_TTL_SECONDS", "45")),
            tombstone_ttl_seconds=tombstone_ttl_seconds,
        )
