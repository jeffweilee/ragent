"""`/pat/v1` router — Personal Access Token authorization (T-PAT).

`POST /pat/v1/authorize` mints a PAT via the init service on-behalf-of the
caller's inbound SSO id token (read from the configured JWT header, never a body
field), then verifies + binds + encrypts + stores it. The SSO identity (nt)
comes from `Depends(get_user_id)`. A minted PAT that fails verification or whose
`PAT_NT_KEY_NAME` claim ≠ the caller — or an init failure — surfaces as the
service's typed error (401 / 429 / 500 / 503). See `docs/spec/pat.md`.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ragent.auth.deps import get_user_id, id_token_of
from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.services.pat_service import (
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
    PatReauthRequired,
    PatService,
)

logger = structlog.get_logger(__name__)

# Init/authorize failures the service surfaces; each carries the `error_code` +
# `http_status` pair required by 00_rule.md §API Error Honesty.
_AUTHORIZE_ERRORS = (
    PatTokenInvalid,
    PatReauthRequired,
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
)


def create_pat_router(*, pat_service: PatService, id_token_header_name: str) -> APIRouter:
    router = APIRouter(prefix="/pat/v1")

    @router.post("/authorize", status_code=204)
    async def authorize(
        request: Request,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Response:
        if not user_id:
            return problem(422, HttpErrorCode.MISSING_USER_ID, "missing user identity")
        id_token = id_token_of(request, id_token_header_name)
        if not id_token:
            logger.warning("pat.authorize.missing_id_token", user_id=user_id)
            return problem(401, HttpErrorCode.PAT_REAUTH_REQUIRED, "missing SSO id token")
        try:
            await pat_service.authorize(nt=user_id, id_token=id_token)
        except _AUTHORIZE_ERRORS as exc:
            return problem(exc.http_status, exc.error_code, "PAT authorization failed")
        return Response(status_code=204)

    return router
