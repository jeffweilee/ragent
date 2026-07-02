"""T-CAv3N — NATS publisher for live session-list status (app-flow auth).

Mirrors mco-clean's NATS connection (ephemeral Ed25519 nkey → JWT exchange →
challenge-response on connect), but uses the **backend "app" auth flow** instead
of the frontend's per-user "tsso" flow:

  1. Generate an ephemeral Ed25519 user nkey (public key + seed), kept in memory.
  2. POST the NATS auth service with the app's `client_secret` + `namespace` + the
     public key → receive a short-lived NATS user JWT (`natsToken`).
  3. Connect to NATS presenting that JWT and signing the server nonce with the
     ephemeral seed (`user_jwt_cb` + `signature_cb`, exactly as nats-py signs for
     `nkeys_seed`: `base64.b64encode(seed.sign(nonce))`).

ragent publishes a run's start/finish (and a session-open dot-clear) to a per-user
subject the frontend subscribes to. Everything is best-effort / fail-soft: an auth
or connect failure degrades to "snapshot only" (the durable truth is the Redis
run-pointer + unread flag the client re-reads from `sessionList`) and never aborts
boot or fails an HTTP request.

The platform's app JWTs are short-lived (~1 minute), so a background task re-runs
the exchange every ``jwt_refresh_seconds`` (same ephemeral keypair, new token) and
``user_jwt_cb`` always serves the latest one — nats-py re-invokes it on every
(re)connect handshake, so a reconnect after token expiry self-heals instead of
re-presenting the dead boot-time JWT forever. Reconnect attempts are unbounded
(``max_reconnect_attempts=-1``): a backend pod lives for weeks and must ride out
NATS outages longer than nats-py's ~2-minute default give-up window.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from concurrent.futures import Future
from typing import Any

import httpx
import nkeys
import structlog

logger = structlog.get_logger(__name__)

_AUTH_PATH = "/api/v1/auth"


def _new_user_keypair() -> Any:
    """An ephemeral Ed25519 user nkey — public key (`U…`) + seed (`SU…`), in memory."""
    seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER)
    return nkeys.from_seed(seed)


class NatsSessionPublisher:
    """Publishes per-user session-list status events to the shared platform NATS.

    Constructed (with config) in the composition root and connected later in the
    lifespan via :meth:`connect`. When unconfigured — or before connect, or after a
    failed exchange/connect — :meth:`publish` is a no-op, so the live channel degrades
    to "snapshot only" rather than failing a request.

    ``verify_certs`` (default ``True``, same default-secure convention as
    ``ES_VERIFY_CERTS``/``OIDC_VERIFY_SSL``) gates TLS verification on the
    ``_fetch_app_jwt`` POST only — it has no effect on the later NATS connection.
    Set ``False`` only for a dev/self-signed auth-service CA or a broken
    intermediate chain; that POST carries ``client_secret``, so this must stay
    ``True`` anywhere the connection isn't already trusted.
    """

    def __init__(
        self,
        *,
        servers: str | None,
        auth_service_url: str | None,
        client_secret: str | None,
        namespace: str | None,
        subject_template: str = "session.{user}.status",
        verify_certs: bool = True,
        connect_timeout_seconds: float = 10.0,
        jwt_refresh_seconds: float = 30.0,
    ) -> None:
        self._servers = servers
        self._auth_url = auth_service_url.rstrip("/") if auth_service_url else None
        self._client_secret = client_secret
        self._namespace = namespace
        self._subject_template = subject_template
        self._verify_certs = verify_certs
        # nats-py's connect() retries internally (default up to ~2 min) when
        # unreachable; bound the initial attempt so a NATS outage can't stall
        # FastAPI lifespan startup.
        self._connect_timeout_seconds = connect_timeout_seconds
        # Must stay under the platform JWT's TTL (~60s) so a reconnect handshake
        # never presents an expired token.
        self._jwt_refresh_seconds = jwt_refresh_seconds
        self._nc: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._keypair: Any = None
        self._jwt: str | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    def _enabled(self) -> bool:
        return bool(self._servers and self._auth_url and self._client_secret and self._namespace)

    def subject(self, user_id: str) -> str:
        # Operator-configurable via NATS_SESSION_SUBJECT_TEMPLATE; per-user so a
        # subscriber only ever receives their own runs' transitions.
        return self._subject_template.format(user=user_id)

    def _auth_payload(self, public_key: str) -> dict[str, str]:
        # `publicKey` is camelCase to mirror the frontend's nats-auth.ts payload key.
        return {
            "token_type": "app",
            "token": self._client_secret or "",
            "namespace": self._namespace or "",
            "publicKey": public_key,
        }

    async def _fetch_app_jwt(self, public_key: str) -> str:
        async with httpx.AsyncClient(verify=self._verify_certs) as client:
            resp = await client.post(
                f"{self._auth_url}{_AUTH_PATH}",
                json=self._auth_payload(public_key),
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()["natsToken"]

    async def connect(self, loop: asyncio.AbstractEventLoop) -> None:
        """Exchange for a NATS JWT and open the connection (lifespan startup).

        No-op when unconfigured. A failure is logged and swallowed — realtime status
        is best-effort and must not abort boot (the sessionList snapshot still works).
        The connect itself is bounded by ``connect_timeout_seconds`` so an
        unreachable broker can't stall this method (and the lifespan awaiting it)
        far longer than a boot-time step should ever take.
        """
        if not self._enabled():
            return
        try:
            # `_enabled()` only checks the raw string is truthy; a whitespace-only
            # NATS_SERVERS parses to no URLs — guard here so a misconfig skips the
            # wasted JWT POST below and never calls nats.connect([]).
            servers = [s.strip() for s in self._servers.split(",") if s.strip()]  # type: ignore[union-attr]
            if not servers:
                logger.warning("nats.connect_failed", error_type="NoServersConfigured")
                return

            import nats

            self._keypair = _new_user_keypair()
            self._jwt = await self._fetch_app_jwt(self._keypair.public_key.decode())
            self._nc = await asyncio.wait_for(
                nats.connect(
                    servers,
                    name="ragent",
                    # Reads the instance attr, not a snapshot: nats-py re-invokes this
                    # on every (re)connect handshake, so a reconnect after the ~1-min
                    # token TTL presents the refresh task's latest JWT, not the dead
                    # boot-time one.
                    user_jwt_cb=lambda: (self._jwt or "").encode(),
                    signature_cb=lambda nonce: base64.b64encode(self._keypair.sign(nonce.encode())),
                    # Unbounded retries: the default gives up permanently after ~2 min,
                    # which for a weeks-lived pod means snapshot-only until restart.
                    max_reconnect_attempts=-1,
                ),
                timeout=self._connect_timeout_seconds,
            )
            self._loop = loop
            self._refresh_task = loop.create_task(self._refresh_jwt_forever())
            logger.info("nats.connected")
        except Exception as exc:  # noqa: BLE001 — best-effort channel; degrade to snapshot-only
            logger.warning("nats.connect_failed", error_type=type(exc).__name__)
            self._nc = None

    async def _refresh_jwt_forever(self) -> None:
        """Keep ``self._jwt`` fresh so any reconnect handshake presents a live token.

        Re-runs the auth exchange with the SAME ephemeral keypair every
        ``jwt_refresh_seconds`` — only the token rotates, so ``signature_cb`` stays
        valid. A failed refresh keeps the last good token and retries next tick
        (fail-soft): one blip only matters if a reconnect lands inside it.
        """
        while True:
            await asyncio.sleep(self._jwt_refresh_seconds)
            try:
                self._jwt = await self._fetch_app_jwt(self._keypair.public_key.decode())
            except Exception as exc:  # noqa: BLE001 — keep last good JWT, retry next tick
                logger.warning("nats.jwt_refresh_failed", error_type=type(exc).__name__)

    def publish(self, user_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget publish, callable from any thread (incl. the producer pool).

        Schedules the async publish on the app loop and does NOT wait. Fail-soft: a
        serialization error, a closed loop, or a dropped connection only costs this
        one live nudge — the client re-syncs from the sessionList snapshot. A failure
        inside the scheduled coroutine itself (e.g. the connection drops mid-send)
        lands on the returned Future, not this method's own try/except, so it needs
        its own done-callback to avoid vanishing with no log at all.
        """
        if self._nc is None or self._loop is None:
            return
        try:
            data = json.dumps(event).encode()
            subject = self.subject(user_id)
            future = asyncio.run_coroutine_threadsafe(self._nc.publish(subject, data), self._loop)
            future.add_done_callback(self._log_publish_failure)
        except Exception as exc:  # noqa: BLE001 — best-effort live nudge
            logger.warning("nats.session_publish_failed", error_type=type(exc).__name__)

    @staticmethod
    def _log_publish_failure(future: Future[None]) -> None:
        # A cancelled future (e.g. an outstanding task cancelled during event-loop
        # shutdown) makes .exception() raise CancelledError rather than return it.
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.warning("nats.session_publish_failed", error_type=type(exc).__name__)

    async def close(self) -> None:
        """Drain the connection on lifespan shutdown (flushes buffered publishes)."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._nc is None:
            return
        try:
            await self._nc.drain()
        except Exception:  # noqa: BLE001 — shutdown path; log and continue
            logger.warning("nats.close_failed", exc_info=True)
        finally:
            self._nc = None
