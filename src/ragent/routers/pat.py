"""`/pat/v1` router — Personal Access Token authorization (T-PAT).

`POST /pat/v1/authorize` binds a user-supplied PAT to the resolved SSO identity
(`Depends(get_user_id)` — never a body field), verifies + encrypts + stores it.
A PAT that fails verification or whose `PAT_NT_KEY_NAME` claim ≠ the caller
surfaces as `401 PAT_REAUTH_REQUIRED`. See `docs/spec/pat.md`.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ragent.auth.deps import get_user_id
from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.pat import PatAuthorizeRequest
from ragent.services.pat_service import PatService

logger = structlog.get_logger(__name__)


def create_pat_router(*, pat_service: PatService) -> APIRouter:
    router = APIRouter(prefix="/pat/v1")

    @router.post("/authorize", status_code=204)
    async def authorize(
        body: PatAuthorizeRequest,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Response:
        if not user_id:
            return problem(422, HttpErrorCode.MISSING_USER_ID, "missing user identity")
        try:
            await pat_service.authorize(nt=user_id, pat_token=body.pat_token)
        except PatTokenInvalid as exc:
            return problem(exc.http_status, exc.error_code, "PAT re-authorization required")
        return Response(status_code=204)

    return router
