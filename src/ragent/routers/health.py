"""T7.8 — Health endpoints: /livez, /readyz, /metrics (B4, C9)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import ragent.bootstrap.metrics  # noqa: F401  (registers metrics on default registry)
from ragent.errors.problem import problem
from ragent.routers.health_probes import run_probe

ProbeFn = Callable[[], Awaitable[None]]


def create_health_router(probes: dict[str, ProbeFn] | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        if not probes:
            return JSONResponse(
                {"status": "degraded", "reason": "probes not configured"},
                status_code=503,
            )
        # Run probes concurrently so total latency is bounded by the slowest single
        # probe rather than N × READYZ_PROBE_TIMEOUT_SECONDS.
        names = list(probes.keys())
        outcomes = await asyncio.gather(*(run_probe(n, probes[n]) for n in names))
        for name, failure in zip(names, outcomes, strict=True):
            if failure is not None:
                return problem(
                    503,
                    error_code=failure.error_code,
                    title="readiness probe failed",
                    detail=f"{name}: {failure.detail}",
                )
        return JSONResponse({"status": "ok"})

    return router
