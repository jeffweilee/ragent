"""T7.8 — Health endpoints: /livez, /readyz, /metrics (B4, C9)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import ragent.bootstrap.telemetry  # noqa: F401
from ragent.errors.problem import problem
from ragent.routers.health_probes import run_probe

ProbeFn = Callable[[], Awaitable[None]]


def create_health_router(probes: dict[str, ProbeFn] | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get("/readyz")
    async def readyz():
        if not probes:
            return JSONResponse(
                {"status": "degraded", "reason": "probes not configured"},
                status_code=503,
            )
        for name, probe in probes.items():
            failure = await run_probe(probe)
            if failure is not None:
                return problem(
                    503,
                    error_code=failure.error_code,
                    title="readiness probe failed",
                    detail=f"{name}: {failure.detail}",
                )
        return JSONResponse({"status": "ok"})

    @router.get("/metrics")
    async def metrics() -> PlainTextResponse:
        data = generate_latest()
        return PlainTextResponse(data.decode(), media_type=CONTENT_TYPE_LATEST)

    return router
