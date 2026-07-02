"""T-CAv3R — Redis Stream buffer that makes a /chatagent/v3 run resumable.

A run's SSE frames are tee'd into a Redis Stream keyed by
``chatstream:{user_id}:{thread_id}:{run_id}`` by a background producer that is
decoupled from the client connection, so generation completes even if the
client refreshes. A consumer (the original POST response or a later
``GET /chatagent/v3/reconnect``) replays the stream from a cursor — exclusive of
the client's ``Last-Event-ID`` — so a reconnect resumes exactly where the drop
happened. The key carries the owner so a run cannot be reconnected by guessing
its run_id, and a short TTL bounds how long a finished run stays resumable.

Reads use ``XRANGE`` (not blocking ``XREAD``) so the consumer polls — this keeps
the cursor logic identical across the live POST stream and a cross-pod reconnect
(the producer may run on a different replica), and avoids relying on blocking
semantics that the in-memory test double does not honour.
"""

from __future__ import annotations

import os
import re
from typing import Any

import redis as redis_lib
import structlog

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "chatstream:"
_CURRENT_PREFIX = "chatcurrent:"  # per-thread pointer → the latest run_id
_UNREAD_PREFIX = "chatunread:"  # per-thread flag → a completed run the user hasn't opened
_FROM_START = ("0", "-", "", None)
_FIELD_FRAME = "frame"  # XADD field holding one SSE frame string
_FIELD_EOS = "eos"  # XADD field marking the terminal sentinel (no frame)
_STREAM_ID_RE = re.compile(r"^\d+-\d+$")  # Redis entry id: <ms>-<seq>


class ChatStreamStore:
    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = 300,
        maxlen: int = 10_000,
        unread_ttl_seconds: int = 2_592_000,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._maxlen = maxlen
        # The new-reply flag outlives a run buffer (which is TTL-bound to resumability);
        # it must survive until the user next opens the session, so it gets its own long TTL.
        self._unread_ttl = unread_ttl_seconds

    @staticmethod
    def key(user_id: str, thread_id: str, stream_id: str) -> str:
        # stream_id is a SERVER-minted per-run id (not the client run_id), so a
        # repeated run_id never collides into the same buffer.
        return f"{_KEY_PREFIX}{user_id}:{thread_id}:{stream_id}"

    @staticmethod
    def _lock_key(key: str) -> str:
        return f"{key}:lock"

    @staticmethod
    def _userinput_key(key: str) -> str:
        return f"{key}:user"

    @staticmethod
    def _current_key(user_id: str, thread_id: str) -> str:
        # Distinct prefix (not a suffix on the buffer key) so a client-supplied
        # run_id can never collide with the pointer.
        return f"{_CURRENT_PREFIX}{user_id}:{thread_id}"

    @staticmethod
    def _unread_key(user_id: str, thread_id: str) -> str:
        return f"{_UNREAD_PREFIX}{user_id}:{thread_id}"

    @staticmethod
    def is_from_start(last_id: str | None) -> bool:
        """True for a from-start replay cursor (`None`/``""``/``"0"``/``"-"``).

        Note ``"0"`` and ``"-"`` are truthy strings, so callers must use this, not
        a plain falsiness check, to decide whether to replay the user turn.
        """
        return last_id in _FROM_START

    @staticmethod
    def is_valid_cursor(last_id: str | None) -> bool:
        """A start sentinel or a well-formed Redis entry id.

        ``last_id`` is client-supplied (the ``Last-Event-ID`` header); a malformed
        value would make XRANGE raise ``ResponseError``, so the caller rejects it
        up front rather than 500.
        """
        return last_id in _FROM_START or bool(_STREAM_ID_RE.match(last_id or ""))

    def try_start(self, key: str) -> bool | None:
        """Elect the single producer for a run.

        ``True`` → this caller is the producer; ``False`` → another already is;
        ``None`` → the stream Redis is unreachable, so the caller should take the
        legacy connection-bound path instead of breaking the request.
        """
        try:
            return bool(self._redis.set(self._lock_key(key), "1", nx=True, ex=self._ttl))
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="try_start", error=str(exc))
            return None

    def set_current(self, user_id: str, thread_id: str, stream_id: str) -> None:
        """Point the thread at its latest run so reconnect resolves it server-side.

        The reconnect endpoint trusts this server-minted stream_id, not a client
        run_id (which the client may reuse). Best-effort: a Redis blip here only
        costs resumability of this run, never the request. The pointer's TTL is set
        here (not at completion), so — like the producer lock — a single run that
        streams longer than the TTL stops being reconnectable mid-flight; once it
        finishes it is served from session history instead.
        """
        try:
            self._redis.set(self._current_key(user_id, thread_id), stream_id, ex=self._ttl)
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="set_current", error=str(exc))

    def get_current(self, user_id: str, thread_id: str) -> str | None:
        try:
            return self._redis.get(self._current_key(user_id, thread_id))
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="get_current", error=str(exc))
            return None

    def stash_user_input(self, key: str, text: str) -> None:
        """Keep the run's user turn so reconnect can replay it.

        The live stream only carries the assistant side; without this, a client
        that lost its local state on refresh would see the answer with no question.
        """
        try:
            self._redis.set(self._userinput_key(key), text, ex=self._ttl)
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="stash_user_input", error=str(exc))

    def get_user_input(self, key: str) -> str | None:
        try:
            return self._redis.get(self._userinput_key(key))
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="get_user_input", error=str(exc))
            return None

    def is_done(self, key: str) -> bool:
        """True once the run has finished — the ``eos`` sentinel is the last entry.

        reconnect serves only a *still-running* run. A finished run is (within the
        fast upstream write) already in session, so reconnect returns expired and
        the client takes it from `GET /session` — no buffer/session overlap to
        de-duplicate. The buffer may linger briefly for the live consumer to drain;
        this check, not the buffer's existence, decides reconnect.
        """
        try:
            tail = self._redis.xrevrange(key, max="+", min="-", count=1)
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="is_done", error=str(exc))
            return False
        return bool(tail) and _FIELD_EOS in tail[0][1]

    def is_running(self, user_id: str, thread_id: str) -> bool:
        """True while the thread's current run is still streaming (pointer set, no eos).

        Derived from the existing run pointer + buffer state, so the session list can
        show a spinner with no extra bookkeeping: a finished run writes ``eos``
        (``is_done`` → True) and an abandoned pointer simply expires with its TTL.
        Fail-soft via ``get_current``/``is_done`` (both return the safe default on a
        Redis blip), so a list fetch never 500s on the spinner.

        The ``is_resumable`` gate avoids a ghost spinner: if the pointer outlives its
        run's buffer+lock (e.g. a producer died before ``eos``), ``is_done`` would see
        an empty stream and read False → "running" forever until the pointer TTL.
        ``is_resumable`` is the same liveness predicate ``reconnect`` uses, so a run is
        "running" here iff it is still reconnectable there.
        """
        run_id = self.get_current(user_id, thread_id)
        if run_id is None:
            return False
        key = self.key(user_id, thread_id, run_id)
        if not self.is_resumable(key):
            return False
        return not self.is_done(key)

    def mark_unread(self, user_id: str, thread_id: str) -> None:
        """Flag a completed reply the user has not opened yet (fail-soft).

        A plain presence flag, not a timestamp — ``has_unread`` is a simple
        ``EXISTS`` with no clock comparison. Cleared only by the client's explicit
        mark-read (``POST /session/read``); the backend never infers "read".
        """
        try:
            self._redis.set(self._unread_key(user_id, thread_id), "1", ex=self._unread_ttl)
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="mark_unread", error=str(exc))

    def clear_unread(self, user_id: str, thread_id: str) -> bool:
        """Drop the new-reply flag on the client's explicit mark-read (fail-soft).

        Returns True only when a flag was actually deleted — the DEL count is free
        change detection, letting the caller skip broadcasting a no-op cleared-dot
        delta on repeat mark-reads. False on a Redis blip (safe default: no
        broadcast; the flag, if any, survives for the next attempt).
        """
        try:
            return bool(self._redis.delete(self._unread_key(user_id, thread_id)))
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="clear_unread", error=str(exc))
            return False

    def has_unread(self, user_id: str, thread_id: str) -> bool:
        try:
            return bool(self._redis.exists(self._unread_key(user_id, thread_id)))
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="has_unread", error=str(exc))
            return False

    def status_many(self, user_id: str, thread_ids: list[str]) -> dict[str, dict[str, bool]]:
        """Batch ``{running, hasNewReply}`` for a session list in 2 Redis round-trips.

        Replaces N×(``is_running`` + ``has_unread``) — i.e. up to 3N round-trips — with
        two pipelines: round 1 reads every thread's run pointer; round 2 pipelines the
        per-thread unread ``EXISTS`` plus, for threads that have a pointer, the buffer
        liveness (``EXISTS`` for is_resumable + ``XREVRANGE`` tail for is_done). The
        running rule mirrors :meth:`is_running` (pointer + resumable + not eos). Fail-soft:
        a Redis blip yields all-False so the list still renders (title-only).
        """
        result = {t: {"running": False, "hasNewReply": False} for t in thread_ids}
        if not thread_ids:
            return result
        try:
            ptr = self._redis.pipeline()
            for t in thread_ids:
                ptr.get(self._current_key(user_id, t))
            run_ids = ptr.execute()

            live: list[str] = []
            batch = self._redis.pipeline()
            for t in thread_ids:
                batch.exists(self._unread_key(user_id, t))
            for t, run_id in zip(thread_ids, run_ids, strict=True):
                if run_id is not None:
                    key = self.key(user_id, t, run_id)
                    # Split the is_resumable check into two single-key EXISTS: the buffer
                    # and lock keys hash to different cluster slots, so a multi-key EXISTS
                    # would raise CROSSSLOT under Redis Cluster (free here — same pipeline).
                    batch.exists(key)
                    batch.exists(self._lock_key(key))
                    batch.xrevrange(key, max="+", min="-", count=1)  # is_done tail
                    live.append(t)
            res = batch.execute()
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="status_many", error=str(exc))
            return result

        for t, unread in zip(thread_ids, res[: len(thread_ids)], strict=True):
            result[t]["hasNewReply"] = bool(unread)
        idx = len(thread_ids)
        for t in live:
            resumable = bool(res[idx]) or bool(res[idx + 1])  # buffer or lock present
            tail = res[idx + 2]
            idx += 3
            is_done = bool(tail) and _FIELD_EOS in tail[0][1]
            result[t]["running"] = resumable and not is_done
        return result

    def append(self, key: str, frame: str) -> str:
        """Buffer one SSE frame; returns the entry id used as the SSE ``id:``."""
        return self._redis.xadd(key, {_FIELD_FRAME: frame}, maxlen=self._maxlen, approximate=True)

    def mark_done(self, key: str) -> None:
        """Close the stream: an ``eos`` sentinel tells consumers to stop, then bound the TTL."""
        pipe = self._redis.pipeline()
        pipe.xadd(key, {_FIELD_EOS: "1"}, maxlen=self._maxlen, approximate=True)
        pipe.expire(key, self._ttl)
        pipe.execute()

    def is_resumable(self, key: str) -> bool:
        """True if the run has buffered frames OR a producer holds its start lock.

        The lock check closes the startup race: a reconnect can land after the
        POST took the lock but before the producer wrote its first frame, when the
        stream key does not exist yet — the run is still alive and reconnectable.
        Fail-soft on a Redis outage so reconnect degrades to STREAM_EXPIRED (the
        client falls back to session history) rather than 500.
        """
        try:
            return bool(self._redis.exists(key, self._lock_key(key)))
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="is_resumable", error=str(exc))
            return False

    def read_after(self, key: str, last_id: str | None) -> list[tuple[str, str | None]]:
        """Entries strictly after ``last_id`` as ``(entry_id, frame)`` pairs.

        ``frame`` is ``None`` for the terminal ``eos`` sentinel — so consumers
        never touch the Redis field names. ``last_id`` is exclusive (Last-Event-ID
        semantics): xrange min is inclusive, so the cursor entry itself is dropped.
        Fail-soft on a transient Redis outage (returns no entries) so a blip ends
        the stream via the consumer's idle timeout instead of crashing it.
        """
        try:
            if last_id in _FROM_START:
                entries = self._redis.xrange(key, min="-", max="+")
            else:
                entries = [
                    e for e in self._redis.xrange(key, min=last_id, max="+") if e[0] != last_id
                ]
        except redis_lib.RedisError as exc:
            logger.warning("chat_stream_store.unavailable", op="read_after", error=str(exc))
            return []
        return [
            (eid, fields.get(_FIELD_FRAME) if _FIELD_EOS not in fields else None)
            for eid, fields in entries
        ]

    @classmethod
    def from_env(cls) -> ChatStreamStore:
        ttl = int(os.environ.get("REDIS_STREAM_TTL_SECONDS", "300"))
        maxlen = int(os.environ.get("REDIS_STREAM_MAXLEN", "10000"))
        unread_ttl = int(os.environ.get("REDIS_UNREAD_TTL_SECONDS", "2592000"))
        mode = os.environ.get("REDIS_MODE", "standalone")
        if mode == "sentinel":
            from redis.sentinel import Sentinel

            hosts_raw = os.environ.get("REDIS_SENTINEL_HOSTS", "")
            master = os.environ.get("REDIS_STREAM_SENTINEL_MASTER", "stream-master")
            sentinels = [
                (h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1]))
                for h in hosts_raw.split(",")
                if h.strip()
            ]
            sentinel = Sentinel(sentinels)
            client = sentinel.master_for(master, decode_responses=True)
            return cls(client, ttl_seconds=ttl, maxlen=maxlen, unread_ttl_seconds=unread_ttl)

        url = os.environ.get("REDIS_STREAM_URL", "redis://localhost:6379/2")
        return cls(
            redis_lib.from_url(url, decode_responses=True),
            ttl_seconds=ttl,
            maxlen=maxlen,
            unread_ttl_seconds=unread_ttl,
        )
