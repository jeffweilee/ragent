"""T-CAv3N — NATS publisher for live session-list status (running / new-reply).

Server-authoritative delta channel: ragent publishes a run's start/finish (and a
session-open dot-clear) to a per-user subject; the frontend subscribes over its
own already-open NATS connection and merges the delta onto its ``sessionList``
snapshot. Best-effort — a publish failure only costs one live nudge, because the
durable truth is the Redis run-pointer + unread flag, which the client re-reads
from ``sessionList`` on its next fetch (NATS core pub/sub is lossy, so the client
must re-sync the snapshot on (re)connect anyway).

Why a thread-safe fire-and-forget ``publish``: a run's start/finish events are
emitted from the decoupled producer **thread** (`_run_producer`), not the event
loop, so the publish is scheduled onto the app loop via ``run_coroutine_threadsafe``
and never awaited. The connection is opened in the FastAPI lifespan (async),
while its config is read once in the composition root (the only env seam).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class NatsSessionPublisher:
    """Publishes per-user session-list status events to NATS.

    Constructed (with config) in the composition root and connected later in the
    lifespan via :meth:`connect`. When ``servers`` is unset — or before connect,
    or after a connection drop — :meth:`publish` is a no-op, so the live channel
    degrades to "snapshot only" rather than failing a request.
    """

    def __init__(
        self,
        *,
        servers: str | None,
        subject_prefix: str = "session",
        token: str | None = None,
        creds: str | None = None,
    ) -> None:
        self._servers = servers
        self._prefix = subject_prefix
        self._token = token
        self._creds = creds
        self._nc: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def subject(prefix: str, user_id: str) -> str:
        # Per-user subject: session-list status is private, so a user only ever
        # subscribes to (and receives) their own runs' transitions.
        return f"{prefix}.{user_id}.status"

    async def connect(self, loop: asyncio.AbstractEventLoop) -> None:
        """Open the connection (lifespan startup). No-op when NATS is unconfigured.

        A connect failure is logged and swallowed — realtime status is best-effort
        and must not abort boot (the sessionList snapshot still works without it).
        """
        if not self._servers:
            return
        try:
            import nats

            opts: dict[str, Any] = {}
            if self._token:
                opts["token"] = self._token
            if self._creds:
                opts["user_credentials"] = self._creds
            self._nc = await nats.connect(self._servers.split(","), name="ragent", **opts)
            self._loop = loop
            logger.info("nats.connected")
        except Exception as exc:  # noqa: BLE001 — best-effort channel; degrade to snapshot-only
            logger.warning("nats.connect_failed", error_type=type(exc).__name__)
            self._nc = None

    def publish(self, user_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget publish, callable from any thread (incl. the producer pool).

        Schedules the async publish on the app loop and does NOT wait. Fail-soft: a
        serialization error, a closed loop, or a dropped connection only costs this
        one live nudge — the client re-syncs from the sessionList snapshot.
        """
        if self._nc is None or self._loop is None:
            return
        try:
            data = json.dumps(event).encode()
            subject = self.subject(self._prefix, user_id)
            asyncio.run_coroutine_threadsafe(self._nc.publish(subject, data), self._loop)
        except Exception as exc:  # noqa: BLE001 — best-effort live nudge
            logger.warning("nats.session_publish_failed", error_type=type(exc).__name__)

    async def close(self) -> None:
        """Drain the connection on lifespan shutdown (flushes buffered publishes)."""
        if self._nc is None:
            return
        try:
            await self._nc.drain()
        except Exception:  # noqa: BLE001 — shutdown path; log and continue
            logger.warning("nats.close_failed", exc_info=True)
        finally:
            self._nc = None
