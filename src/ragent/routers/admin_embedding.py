"""Admin embedding-lifecycle router (T-EM.13, B50 §5).

Mounted at `/embedding/v1`. Thin parse → delegate → problem layer
per CLAUDE.md §Modules. Five lifecycle endpoints, GET /state, GET /preflight.

Exception → HTTP mapping (all 4xx responses are RFC 9457 problem+json):
- `IllegalEmbeddingTransition`   → 409 EMBEDDING_LIFECYCLE_INVALID_STATE
- `CutoverPreflightFailed`       → 409 EMBEDDING_CUTOVER_PREFLIGHT_FAILED (body carries report)
- `InvalidEmbeddingModelConfig`  → 422 EMBEDDING_INVALID_CONFIG
- `EmbeddingFieldCollision`      → 422 EMBEDDING_FIELD_NAME_COLLISION
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ragent.clients.embedding_model_config import InvalidEmbeddingModelConfig
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.services.active_model_registry import ActiveModelRegistryNotReady
from ragent.services.embedding_lifecycle_service import (
    CutoverPreflightFailed,
    EmbeddingFieldCollision,
)
from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition


class PromoteRequest(BaseModel):
    name: str = Field(min_length=1)
    dim: int
    api_url: str = Field(min_length=1)
    model_arg: str = Field(min_length=1)


class CutoverRequest(BaseModel):
    force: bool = False


_EXCEPTION_MAP: list[tuple[type, int, HttpErrorCode]] = [
    (IllegalEmbeddingTransition, 409, HttpErrorCode.EMBEDDING_LIFECYCLE_INVALID_STATE),
    (InvalidEmbeddingModelConfig, 422, HttpErrorCode.EMBEDDING_INVALID_CONFIG),
    (EmbeddingFieldCollision, 422, HttpErrorCode.EMBEDDING_FIELD_NAME_COLLISION),
    (ActiveModelRegistryNotReady, 503, HttpErrorCode.EMBEDDING_REGISTRY_NOT_READY),
]


def _to_problem(exc: Exception) -> JSONResponse | None:
    if isinstance(exc, CutoverPreflightFailed):
        return problem(
            409,
            HttpErrorCode.EMBEDDING_CUTOVER_PREFLIGHT_FAILED,
            "Cutover preflight failed",
            extra={"preflight": exc.report},
        )
    for exc_type, status, code in _EXCEPTION_MAP:
        if isinstance(exc, exc_type):
            return problem(status, code, str(exc) or code.value)
    return None


def _handle_exceptions(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            mapped = _to_problem(exc)
            if mapped is not None:
                return mapped
            raise

    return wrapper


def create_router(*, service: Any, snapshot_provider: Callable[[], dict]) -> APIRouter:
    router = APIRouter(prefix="/embedding/v1")

    @router.post("/promote")
    @_handle_exceptions
    async def promote(req: PromoteRequest):
        return await service.promote(
            name=req.name, dim=req.dim, api_url=req.api_url, model_arg=req.model_arg
        )

    @router.post("/cutover")
    @_handle_exceptions
    async def cutover(req: CutoverRequest):
        return await service.cutover(force=req.force)

    @router.post("/rollback")
    @_handle_exceptions
    async def rollback():
        return await service.rollback()

    @router.post("/commit")
    @_handle_exceptions
    async def commit():
        return await service.commit()

    @router.post("/abort")
    @_handle_exceptions
    async def abort():
        return await service.abort()

    @router.get("/state")
    @_handle_exceptions
    async def state():
        return snapshot_provider()

    @router.get("/cutover/preflight")
    async def preflight():
        return await service.preflight()

    return router
