"""`/skills/v1` router — owner-scoped CRUD for user skill presets (T-SK).

Every handler scopes to the resolved ``user_id`` (``Depends(get_user_id)``) and
passes it straight to the service, so one user can never read or mutate
another's skills. The owner is never taken from the request body.

Validation failures surface as RFC 9457 problem+json with
``error_code=SKILL_VALIDATION`` (mirrors ``routers/feedback._FeedbackRoute``),
not FastAPI's default ``{"detail": [...]}`` shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute

from ragent.auth.deps import get_user_id
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.skill import SkillListResponse, SkillResponse, SkillWriteRequest
from ragent.services.skill_service import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillService,
)

logger = structlog.get_logger(__name__)


def _validation_problem(errors: list[dict]) -> Response:
    fields = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in errors]
    return problem(
        422,
        HttpErrorCode.SKILL_VALIDATION,
        "skill request validation failed",
        "; ".join(f"{f['field']}: {f['message']}" for f in fields),
        errors=fields,
    )


def _require_user(user_id: str | None) -> Response | None:
    """Return a 422 problem when no user identity resolved; ``None`` otherwise.

    Skills are per-user, so an unscoped request cannot be served. Production
    traffic always carries an identity (middleware), but the bare-router unit
    path and misconfigured callers must fail closed rather than touch the table
    with a null owner.
    """
    if user_id:
        return None
    return problem(422, HttpErrorCode.MISSING_USER_ID, "missing user identity")


class _SkillRoute(APIRoute):
    """Render FastAPI ``RequestValidationError`` as SKILL_VALIDATION problem+json."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Any) -> Any:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return _validation_problem(exc.errors())

        return handler


def create_skill_router(*, skill_service: SkillService) -> APIRouter:
    router = APIRouter(prefix="/skills/v1", route_class=_SkillRoute)

    @router.post("", status_code=201)
    async def create_skill(
        body: SkillWriteRequest,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Any:
        if (rejected := _require_user(user_id)) is not None:
            return rejected
        try:
            return await skill_service.create(
                user_id=user_id,
                name=body.name,
                description=body.description,
                instructions=body.instructions,
                enabled=body.enabled,
            )
        except SkillNameConflictError as exc:
            return problem(exc.http_status, exc.error_code, "skill name already exists", str(exc))

    @router.get("", response_model=SkillListResponse)
    async def list_skills(
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Any:
        if (rejected := _require_user(user_id)) is not None:
            return rejected
        return SkillListResponse(skills=await skill_service.list_for_user(user_id=user_id))

    @router.get("/{skill_id}", response_model=SkillResponse)
    async def get_skill(
        skill_id: str,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Any:
        if (rejected := _require_user(user_id)) is not None:
            return rejected
        try:
            return await skill_service.get(user_id=user_id, skill_id=skill_id)
        except SkillNotFoundError as exc:
            return problem(exc.http_status, exc.error_code, "skill not found", str(exc))

    @router.put("/{skill_id}", response_model=SkillResponse)
    async def update_skill(
        skill_id: str,
        body: SkillWriteRequest,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Any:
        if (rejected := _require_user(user_id)) is not None:
            return rejected
        try:
            return await skill_service.update(
                user_id=user_id,
                skill_id=skill_id,
                name=body.name,
                description=body.description,
                instructions=body.instructions,
                enabled=body.enabled,
            )
        except SkillNotFoundError as exc:
            return problem(exc.http_status, exc.error_code, "skill not found", str(exc))
        except SkillNameConflictError as exc:
            return problem(exc.http_status, exc.error_code, "skill name already exists", str(exc))

    @router.delete("/{skill_id}", status_code=204)
    async def delete_skill(
        skill_id: str,
        user_id: Annotated[str | None, Depends(get_user_id)] = None,
    ) -> Response:
        if (rejected := _require_user(user_id)) is not None:
            return rejected
        try:
            await skill_service.delete(user_id=user_id, skill_id=skill_id)
        except SkillNotFoundError as exc:
            return problem(exc.http_status, exc.error_code, "skill not found", str(exc))
        return Response(status_code=204)

    return router
