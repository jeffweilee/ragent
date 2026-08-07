"""`/pat/v1` router — Personal Access Token authorization (T-PAT).

`POST /pat/v1/authorize` takes TWO tokens and no body:

* the **access token** the auth middleware already verified, which resolves the
  caller (`Depends(get_user_id)` — never a body field), and
* an **SSO id token** on the fixed `X-Id-Token` header, which is what the PAT
  init service wants as its on-behalf-of credential.

They are different tokens: ragent authenticates with the access token, while
init mints against the id token. The id token is verified HERE with the same
JWKS / issuer / audience as the access token, and its username claim must equal
the resolved caller — so a valid id token belonging to somebody else cannot mint
a PAT, and a junk token never reaches init (which rate-limits at 10/60 s per
client + nt). A failure at any step surfaces as the PAT slice's typed error
(401 / 429 / 500 / 503). See `docs/spec/pat.md`.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header
from fastapi.responses import Response

from ragent.auth.deps import get_user_id
from ragent.auth.jwt import JwtAuthError, VerifyingTokenManager, verify_jwt
from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.pat import PatStatusResponse
from ragent.services.pat_service import (
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
    PatReauthRequired,
    PatService,
)

logger = structlog.get_logger(__name__)

# Fixed part of this endpoint's contract, not deployment config: `/pat/v1` is
# our own surface, so the name is pinned here (and exported for tests) rather
# than threaded through an env var. Declaring it as a real `Header` parameter
# also publishes it in the OpenAPI schema for free.
ID_TOKEN_HEADER = "X-Id-Token"

# Init/authorize failures the service surfaces; each carries the `error_code` +
# `http_status` pair required by 00_rule.md §API Error Honesty.
_AUTHORIZE_ERRORS = (
    PatTokenInvalid,
    PatReauthRequired,
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
)


def create_pat_router(
    *, pat_service: PatService, token_manager: VerifyingTokenManager, jwt_claim_user_id: str
) -> APIRouter:
    router = APIRouter(prefix="/pat/v1")

    @router.post("/authorize", status_code=204)
    async def authorize(
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
        id_token: Annotated[str | None, Header(alias=ID_TOKEN_HEADER)] = None,
    ) -> Response:
        if not user_id:
            logger.warning(
                "pat.authorize.rejected",
                reason="missing_user_id",
                error_code=HttpErrorCode.MISSING_USER_ID,
            )
            return problem(422, HttpErrorCode.MISSING_USER_ID, "missing user identity")
        if not id_token:
            return _reject(user_id, "missing_id_token")
        try:
            id_token_user = verify_jwt(
                id_token, claim_user_id=jwt_claim_user_id, token_manager=token_manager
            )
        except JwtAuthError as exc:
            # Deliberately collapsed to PAT_REAUTH_REQUIRED: the remedy is the
            # same for every id-token failure (re-authorize), and the granular
            # reason rides the log instead of telling a caller which part failed.
            return _reject(user_id, "id_token_invalid", error_code=exc.error_code)
        if id_token_user != user_id:
            return _reject(user_id, "id_token_owner_mismatch")
        try:
            await pat_service.authorize(nt=user_id, id_token=id_token)
        except _AUTHORIZE_ERRORS as exc:
            return problem(exc.http_status, exc.error_code, "PAT authorization failed")
        return Response(status_code=204)

    @router.get("/status", response_model=PatStatusResponse)
    async def status(
        response: Response,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Any:
        """Report the caller's own authorization state — a pure read.

        `no-store` because this is per-user credential state: a cached `active`
        served after a revoke would tell the user they are still authorized.

        A `404` here means the PAT slice is not wired in this deployment
        (`PAT_PUBLIC_KEY` unset), so the router was never mounted — clients
        should read that as "feature off" and hide the UI, not as an error.
        """
        if not user_id:
            logger.warning(
                "pat.status.rejected",
                reason="missing_user_id",
                error_code=HttpErrorCode.MISSING_USER_ID,
            )
            return problem(422, HttpErrorCode.MISSING_USER_ID, "missing user identity")
        response.headers["Cache-Control"] = "no-store"
        return await pat_service.status(nt=user_id)

    @router.delete("/authorize", status_code=204)
    async def revoke(
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Response:
        """Drop the caller's PAT. No body, and no `X-Id-Token`: the upstream has
        no revoke endpoint, so nothing is called on-behalf-of the user.

        **Idempotent `204`, never `404`** — unlike a skill, the PAT is a
        per-caller singleton (`uq_pat_user`), not an id-addressed object. Asking
        to be un-authorized states a target state, so repeating it (or revoking
        when never authorized) is success; whether a row existed rides
        `pat.revoke.completed(existed=…)` rather than the status code.
        """
        if not user_id:
            logger.warning(
                "pat.revoke.rejected",
                reason="missing_user_id",
                error_code=HttpErrorCode.MISSING_USER_ID,
            )
            return problem(422, HttpErrorCode.MISSING_USER_ID, "missing user identity")
        await pat_service.revoke(nt=user_id)
        return Response(status_code=204)

    return router


def _reject(user_id: str, reason: str, *, error_code: str | None = None) -> Response:
    """401 the caller and log WHY — the response intentionally does not say."""
    logger.warning(
        "pat.authorize.rejected",
        user_id=user_id,
        reason=reason,
        error_code=error_code or HttpErrorCode.PAT_REAUTH_REQUIRED,
    )
    return problem(401, HttpErrorCode.PAT_REAUTH_REQUIRED, "PAT re-authorization required")
