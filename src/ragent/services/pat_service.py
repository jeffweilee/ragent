"""PatService — PAT authorization write + on-demand resolution (T-PAT).

Coordinates the PAT slice: mint (via the init service) + verify + bind + encrypt
+ persist on authorization; redis→DB→refresh→invalidate on resolution; the
refresh state machine (per-nt lock, 401→invalidate, 400→internal,
429/transient→bounded backoff). Init errors map to 401/429/500/503 and are never
retried — the init API is rate limited, so a stored PAT is rotated via refresh
instead. Full flow + contracts: `docs/spec/pat.md`.

Async because the repository rides the async engine; the (fast) redis cache is
called directly (fail-soft), and the (blocking) init + refresh HTTP calls are
offloaded with `run_in_threadpool` so they never stall the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi.concurrency import run_in_threadpool

from ragent.auth.pat_jwt import PatTokenInvalid, PatTokenVerifier
from ragent.clients.pat_cache import PatCache
from ragent.clients.pat_init_client import (
    MintedPat,
    PatInitBadRequest,
    PatInitClient,
    PatInitError,
    PatInitRateLimited,
    PatInitUnauthorized,
)
from ragent.clients.pat_refresh_client import (
    PatRefreshBadRequest,
    PatRefreshClient,
    PatRefreshError,
    PatRefreshUnauthorized,
)
from ragent.errors.codes import HttpErrorCode
from ragent.schemas.pat import PatStatus, PatStatusResponse
from ragent.security.pat_cipher import PATCipher, PATDecryptionError
from ragent.utility.datetime import from_db, to_iso, utcnow

logger = structlog.get_logger(__name__)


class PatReauthRequired(Exception):
    """The stored PAT is unusable and cannot be refreshed — user must re-authorize."""

    error_code = HttpErrorCode.PAT_REAUTH_REQUIRED
    http_status = 401


class PatInternalError(Exception):
    """The refresh / init service rejected our own request (400) — a ragent-side bug."""

    error_code = HttpErrorCode.INTERNAL_ERROR
    http_status = 500


class PatRefreshExhausted(Exception):
    """Refresh kept failing transiently (429/5xx) past the retry budget; the PAT
    stays active and the request is rejected for now."""

    http_status = 503


class PatInitThrottled(Exception):
    """The init service rate-limited the mint (429 — caps 10/60s per client+nt);
    the caller should retry later (downstream keeps + refreshes the PAT)."""

    error_code = HttpErrorCode.PAT_INIT_RATE_LIMITED
    http_status = 429


class PatInitUnavailable(Exception):
    """The init service is transiently unavailable (5xx / transport / malformed)."""

    error_code = HttpErrorCode.PAT_INIT_UNAVAILABLE
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
        init_client: PatInitClient,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lock_poll_attempts: int = 10,
        lock_poll_interval_seconds: float = 0.5,
    ) -> None:
        self._verifier = verifier
        self._cipher = cipher
        self._repo = repo
        self._cache = cache
        self._refresh_client = refresh_client
        self._init_client = init_client
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._sleeper = sleeper
        self._lock_poll_attempts = lock_poll_attempts
        self._lock_poll_interval = lock_poll_interval_seconds

    # --- Part 1: authorization write -------------------------------------
    async def authorize(self, *, nt: str, id_token: str) -> None:
        """Mint a PAT via the init service (on-behalf-of ``id_token``), then
        verify + bind + encrypt + persist it.

        Raises ``PatTokenInvalid`` (401 ``PAT_REAUTH_REQUIRED``) on a bad minted
        token or an nt-binding mismatch, or a mapped init error
        (``PatReauthRequired`` / ``PatInternalError`` / ``PatInitThrottled`` /
        ``PatInitUnavailable``); writes nothing on failure."""
        logger.info("pat.authorize.started", user_id=nt)
        minted = await self._mint(nt, id_token)
        pat_token = minted.token
        # A minted PAT that fails verification means the init service handed us
        # something unusable — log the terminal event so `started` is never left
        # dangling without an outcome (00_rule.md §Service Boundary Logs).
        try:
            claims = self._verifier.verify(pat_token)
            bound_nt = self._verifier.nt_of(claims)
        except PatTokenInvalid:
            logger.warning(
                "pat.authorize.failed",
                user_id=nt,
                error_code=HttpErrorCode.PAT_REAUTH_REQUIRED,
                reason="minted_token_invalid",
            )
            raise
        if bound_nt != nt:
            logger.warning(
                "pat.authorize.failed",
                user_id=nt,
                error_code=HttpErrorCode.PAT_REAUTH_REQUIRED,
                reason="nt_mismatch",
            )
            raise PatTokenInvalid()
        cipher_text = self._cipher.encrypt(pat_token)
        # The window is the one init was actually asked for (carried on the mint
        # result, not recomputed here) — authorize is the only writer, so a later
        # refresh cannot appear to extend it.
        await self._repo.upsert(
            user_id=nt,
            pat_cipher=cipher_text,
            authorization_expires_at=minted.expire_date,
        )
        self._cache.put(nt, cipher_text)
        logger.info(
            "pat.authorize.stored", user_id=nt, authorization_expires_at=str(minted.expire_date)
        )

    async def _mint(self, nt: str, id_token: str) -> MintedPat:
        """Call the init service and map its typed HTTP errors to service errors."""
        try:
            return await run_in_threadpool(self._init_client.init, id_token)
        except PatInitUnauthorized as exc:
            logger.warning(
                "pat.init.unauthorized", user_id=nt, error_code=HttpErrorCode.PAT_REAUTH_REQUIRED
            )
            raise PatReauthRequired() from exc
        except PatInitBadRequest as exc:
            logger.error(
                "pat.init.bad_request", user_id=nt, error_code=HttpErrorCode.INTERNAL_ERROR
            )
            raise PatInternalError("init service rejected our request (400)") from exc
        except PatInitRateLimited as exc:
            logger.warning(
                "pat.init.rate_limited", user_id=nt, error_code=HttpErrorCode.PAT_INIT_RATE_LIMITED
            )
            raise PatInitThrottled() from exc
        except PatInitError as exc:  # transient / transport / malformed
            logger.warning(
                "pat.init.unavailable", user_id=nt, error_code=HttpErrorCode.PAT_INIT_UNAVAILABLE
            )
            raise PatInitUnavailable() from exc

    # --- Revoke -----------------------------------------------------------
    async def revoke(self, *, nt: str) -> None:
        """Drop the caller's authorization. Idempotent — absent is success.

        Order matters at every step:

        1. **Tombstone first.** From here on `cache.put` refuses, so a refresh
           that already rotated cannot republish the token after step 3.
        2. **DB delete before the eviction.** Evicting first would let a
           concurrent `resolve` miss the cache, read the still-present row, and
           re-fill redis — a window one DB round-trip wide. Deleting first makes
           that resolve read `None` and raise instead.
        3. **Evict** whatever was cached before the tombstone landed.

        Redis is fail-soft throughout: if it is down, neither the tombstone nor
        the eviction lands and a cached PAT survives to its TTL. The DB row is
        still gone, so this is bounded and self-healing — it is the one case
        where revocation is not immediate (`docs/spec/pat.md` §已知限制).
        """
        logger.info("pat.revoke.started", user_id=nt)
        self._cache.mark_revoked(nt)
        rowcount = await self._repo.delete(user_id=nt)
        self._cache.evict(nt)
        logger.info("pat.revoke.completed", user_id=nt, existed=rowcount > 0)

    # --- Status (read-only) -----------------------------------------------
    async def status(self, *, nt: str) -> PatStatusResponse:
        """Report the caller's authorization state. **Zero side effects.**

        Deliberately does NOT go through `resolve()`, even though the mapping
        looks similar: `resolve` refreshes an expired PAT, which means an HTTP
        round-trip, a DB write and a possible `mark_invalid`. Routing a
        per-page-load GET through that would turn this endpoint into a request
        amplifier aimed at the refresh service. Nothing here reads or writes
        redis or calls an upstream.

        The invariant is **one-directional**: this must never report `active`
        when `resolve` would fail. The reverse is intended — once past
        `authorization_expires_at` the answer is `invalid` even though `resolve`
        can still hand out a locally-valid token, and that is precisely how the
        window between the upstream dropping the authorization and the next
        refresh-401 reaches the user.
        """
        row = await self._repo.get(user_id=nt)
        result = PatStatusResponse(
            status=self._classify(row, nt),
            authorized_at=_iso_or_none(row.get("authorized_at")) if row else None,
            authorization_expires_at=(
                _date_or_none(row.get("authorization_expires_at")) if row else None
            ),
        )
        logger.info("pat.status.read", user_id=nt, status=result.status)
        return result

    def _classify(self, row: Any, nt: str) -> PatStatus:
        if row is None:
            return "none"
        if row["status"] != "active":
            return "invalid"
        # NOT a column read. `_safe_decrypt` failure (key rotation / corruption)
        # makes `resolve` raise while `pat.status` still says 'active' — no code
        # path ever flips that column — so returning row["status"] here would
        # report a healthy authorization that fails on every single request.
        token = self._safe_decrypt(row["pat_cipher"])
        if token is None:
            return "invalid"
        try:
            # Expiry ignored on purpose: an expired PAT is refreshable and is the
            # normal steady state. Only a structurally broken or wrongly-bound
            # token means the user must act.
            claims = self._verifier.verify(token, ignore_expiry=True)
            if self._verifier.nt_of(claims) != nt:
                return "invalid"
        except PatTokenInvalid:
            return "invalid"
        expires_at = row.get("authorization_expires_at")
        if expires_at is not None and utcnow().date() > expires_at:
            return "invalid"
        return "active"

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
        # Single-flight: only the lock holder calls the refresh service. A loser
        # POLLS (not immediately self-refreshes) — sleeping between tries and
        # returning the winner's rotated token as soon as it lands, so concurrent
        # requests don't stampede the refresh API (gemini review r3619465015).
        lock_token = None
        for _ in range(self._lock_poll_attempts):
            lock_token = self._cache.acquire_refresh_lock(nt)
            if lock_token:
                break
            await self._sleeper(self._lock_poll_interval)
            fresh = self._cached_valid(nt)
            if fresh:
                return fresh
        try:
            # Double-checked locking: the previous holder may have rotated the
            # token while we waited for the lock.
            fresh = self._cached_valid(nt)
            if fresh:
                return fresh
            return await self._do_refresh(nt, current)
        finally:
            if lock_token:
                self._cache.release_refresh_lock(nt, lock_token)

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

            # Verify the rotation before it is persisted — the refresh client only
            # checks the envelope carries a non-empty `patToken`, so without this a
            # malformed or wrongly-signed token would be stored and cached, leaving
            # an `active` row holding a PAT nothing downstream can use.
            try:
                self._verify_rotation(new_token, nt)
            except PatReauthRequired:
                # The raised exception says "re-authorize", so the PERSISTED state
                # has to agree with it. Leaving the row `active` here would make
                # `status` report a healthy authorization while every `resolve`
                # deterministically fails: the stored PAT is already expired (that
                # is why we are refreshing), and each retry asks the same broken
                # upstream for a rotation it will reject again. Re-authorizing is
                # a real remedy — it mints through `init`, not the refresh
                # service — so `invalid` is the honest state.
                await self._repo.mark_invalid(user_id=nt)
                self._cache.evict(nt)
                raise
            cipher_text = self._cipher.encrypt(new_token)
            # UPDATE-only: a revoke that landed while this refresh was in flight
            # deleted the row, and re-creating it would silently undo the user's
            # revocation. rowcount 0 means the authorization is gone.
            if await self._repo.rotate(user_id=nt, pat_cipher=cipher_text) == 0:
                logger.warning(
                    "pat.refresh.revoked",
                    user_id=nt,
                    error_code=HttpErrorCode.PAT_REAUTH_REQUIRED,
                )
                raise PatReauthRequired()
            self._cache.put(nt, cipher_text)
            logger.info("pat.refresh.rotated", user_id=nt)
            return new_token

    # --- helpers ----------------------------------------------------------
    def _verify_rotation(self, token: str, nt: str) -> None:
        """Verify a refreshed PAT and require it to be bound to ``nt``.

        The refresh service is trusted to *issue* a PAT, never to name its owner
        — the same stance `authorize` takes toward the init service. Any failure
        raises ``PatReauthRequired``: re-authorization is the single remedy, and
        nothing is written.
        """
        try:
            bound_nt = self._verifier.nt_of(self._verifier.verify(token))
        except PatTokenInvalid as exc:
            self._reject_rotation(nt, "rotated_token_invalid")
            raise PatReauthRequired() from exc
        if bound_nt != nt:
            self._reject_rotation(nt, "nt_mismatch")
            raise PatReauthRequired()

    @staticmethod
    def _reject_rotation(nt: str, reason: str) -> None:
        logger.warning(
            "pat.refresh.rejected",
            user_id=nt,
            error_code=HttpErrorCode.PAT_REAUTH_REQUIRED,
            reason=reason,
        )

    def _is_valid(self, token: str) -> bool:
        try:
            self._verifier.verify(token)
            return True
        except PatTokenInvalid:
            return False

    def _cached_valid(self, nt: str) -> str | None:
        """The cached PAT if present, decryptable, and locally valid; else None."""
        cached = self._cache.get(nt)
        if cached is None:
            return None
        token = self._safe_decrypt(cached)
        return token if token and self._is_valid(token) else None

    def _safe_decrypt(self, cipher_text: str) -> str | None:
        try:
            return self._cipher.decrypt(cipher_text)
        except PATDecryptionError:
            return None


def _iso_or_none(value: Any) -> str | None:
    """Serialise a DB datetime the way every other ragent API does.

    `from_db` first: the MariaDB driver hands back **naive** datetimes, and
    `to_iso`'s `astimezone` would then interpret them as *local* time — correct
    only by accident on a UTC host, silently wrong by the offset anywhere else.
    """
    return to_iso(from_db(value)) if value is not None else None


def _date_or_none(value: Any) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None else None
