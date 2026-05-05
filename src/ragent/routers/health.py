"""T7.8 — Health endpoints: /livez, /readyz, /metrics (B4, C9)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import ragent.bootstrap.telemetry  # noqa: F401


def create_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        # Full probe implementation requires Docker-backed dependencies (T7.7).
        # Returns 503 until probes are wired; prevents false-positive readiness.
        body = {"status": "degraded", "reason": "probes not configured"}
        return JSONResponse(body, status_code=503)

    @router.get("/metrics")
    async def metrics() -> PlainTextResponse:
        data = generate_latest()
        return PlainTextResponse(data.decode(), media_type=CONTENT_TYPE_LATEST)

    return router
