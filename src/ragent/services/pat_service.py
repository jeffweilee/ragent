"""PatService — PAT authorization write + on-demand resolution (T-PAT).

Coordinates the PAT slice: verify + bind + encrypt + persist on authorization;
redis→DB→refresh→invalidate on resolution; the refresh state machine (per-nt
lock, 401→invalidate, 400→internal, 429/transient→bounded backoff). Full flow +
contracts: `docs/spec/pat.md`.

Async because the repository rides the async engine; the (fast) redis cache is
called directly (fail-soft), and the (blocking) refresh HTTP call is offloaded
with `run_in_threadpool` so it never stalls the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi.concurrency import run_in_threadpool

from ragent.auth.pat_jwt import PatTokenInvalid, PatTokenVerifier
from ragent.clients.pat_cache import PatCache
from ragent.clients.pat_refresh_client import (
    PatRefreshBadRequest,
    PatRefreshClient,
    PatRefreshError,
    PatRefreshUnauthorized,
)
from ragent.errors.codes import HttpErrorCode
from ragent.security.pat_cipher import PATCipher, PATDecryptionError

logger = structlog.get_logger(__name__)


class PatReauthRequired(Exception):
    """The stored PAT is unusable and cannot be refreshed — user must re-authorize."""

    error_code = HttpErrorCode.PAT_REAUTH_REQUIRED
    http_status = 401


class PatInternalError(Exception):
    """The refresh service rejected our own request (400) — a ragent-side bug."""

    http_status = 500


class PatRefreshExhausted(Exception):
    """Refresh kept failing transiently (429/5xx) past the retry budget; the PAT
    stays active and the request is rejected for now."""

    http_status = 503


class PatService:
    def __init__(
        self,
        *,
        verifier: PatTokenVerifier,
        cipher: PATCipher,
        repo: Any,
        cache: PatCache,
        refresh_client: PatRefreshClient,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._verifier = verifier
        self._cipher = cipher
        self._repo = repo
        self._cache = cache
        self._refresh_client = refresh_client
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._sleeper = sleeper

    # --- Part 1: authorization write -------------------------------------
    async def authorize(self, *, nt: str, pat_token: str) -> None:
        """Verify + bind + encrypt + persist a user-supplied PAT.

        Raises ``PatTokenInvalid`` (401 ``PAT_REAUTH_REQUIRED``) on a bad token
        or an nt-binding mismatch; writes nothing on failure."""
        logger.info("pat.authorize.started", user_id=nt)
        claims = self._verifier.verify(pat_token)
        if self._verifier.nt_of(claims) != nt:
            logger.warning("pat.authorize.nt_mismatch", user_id=nt)
            raise PatTokenInvalid()
        cipher_text = self._cipher.encrypt(pat_token)
        await self._repo.upsert(user_id=nt, pat_cipher=cipher_text)
        self._cache.put(nt, cipher_text)
        logger.info("pat.authorize.stored", user_id=nt)

    # --- Request path: resolve a usable PAT ------------------------------
    async def resolve(self, nt: str) -> str:
        """Return a locally-valid PAT for ``nt`` (redis→DB→refresh→invalidate)."""
        cached = self._cache.get(nt)
        if cached is not None:
            token = self._safe_decrypt(cached)
            if token and self._is_valid(token):
                return token
            if token:  # present but expired → rotate
                return await self._refresh(nt, token)

        row = await self._repo.get(user_id=nt)
        if row is None or row["status"] != "active":
            raise PatReauthRequired()
        token = self._safe_decrypt(row["pat_cipher"])
        if token is None:
            raise PatReauthRequired()
        if self._is_valid(token):
            self._cache.put(nt, row["pat_cipher"])
            return token
        return await self._refresh(nt, token)

    async def resolve_best_effort(self, nt: str) -> str | None:
        """Fail-open resolve for the `/brainagent/v1` attach — never raises."""
        if not nt:
            return None
        try:
            return await self.resolve(nt)
        except Exception as exc:  # noqa: BLE001 — attach is additive, must not break the proxy
            logger.info("pat.resolve.unavailable", user_id=nt, error_type=type(exc).__name__)
            return None

    # --- Refresh state machine -------------------------------------------
    async def _refresh(self, nt: str, current: str) -> str:
        acquired = self._cache.acquire_refresh_lock(nt)
        if not acquired:
            # Another refresh is in flight; it may already have written the new
            # token. Re-read the cache before falling back to our own refresh.
            cached = self._cache.get(nt)
            if cached is not None:
                token = self._safe_decrypt(cached)
                if token and self._is_valid(token):
                    return token
        try:
            return await self._do_refresh(nt, current)
        finally:
            if acquired:
                self._cache.release_refresh_lock(nt)

    async def _do_refresh(self, nt: str, current: str) -> str:
        attempt = 0
        while True:
            try:
                new_token = await run_in_threadpool(self._refresh_client.refresh, current)
            except PatRefreshUnauthorized as exc:
                logger.warning("pat.refresh.unauthorized", user_id=nt)
                await self._repo.mark_invalid(user_id=nt)
                self._cache.evict(nt)
                raise PatReauthRequired() from exc
            except PatRefreshBadRequest as exc:
                logger.error("pat.refresh.bad_request", user_id=nt)
                raise PatInternalError("refresh service rejected our request (400)") from exc
            except PatRefreshError as exc:  # rate-limited / transient
                if attempt >= self._max_retries:
                    logger.warning("pat.refresh.exhausted", user_id=nt, attempts=attempt)
                    raise PatRefreshExhausted() from exc
                await self._sleeper(self._backoff_base * (2**attempt))
                attempt += 1
                continue

            cipher_text = self._cipher.encrypt(new_token)
            await self._repo.upsert(user_id=nt, pat_cipher=cipher_text)
            self._cache.put(nt, cipher_text)
            logger.info("pat.refresh.rotated", user_id=nt)
            return new_token

    # --- helpers ----------------------------------------------------------
    def _is_valid(self, token: str) -> bool:
        try:
            self._verifier.verify(token)
            return True
        except PatTokenInvalid:
            return False

    def _safe_decrypt(self, cipher_text: str) -> str | None:
        try:
            return self._cipher.decrypt(cipher_text)
        except PATDecryptionError:
            return None
