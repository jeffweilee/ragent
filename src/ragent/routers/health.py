"""T7.8 — Health endpoints: /livez, /readyz, /metrics (B4, C9)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import ragent.bootstrap.metrics  # noqa: F401  (registers metrics on default registry)
from ragent.errors.problem import problem
from ragent.routers.health_probes import ADVISORY_PROBES, ProbeFailure, run_probe

ProbeFn = Callable[[], Awaitable[None]]

ProbeOutcome = list[tuple[str, ProbeFailure]]


async def _sweep(probes: dict[str, ProbeFn]) -> tuple[ProbeOutcome, ProbeOutcome]:
    """Run every probe concurrently; split the failures into (advisory, required).

    Concurrent so total latency is bounded by the slowest single probe rather
    than N × READYZ_PROBE_TIMEOUT_SECONDS.
    """
    names = list(probes.keys())
    outcomes = await asyncio.gather(*(run_probe(n, probes[n]) for n in names))
    failures = [(n, f) for n, f in zip(names, outcomes, strict=True) if f is not None]
    advisory = [(n, f) for n, f in failures if n in ADVISORY_PROBES]
    required = [(n, f) for n, f in failures if n not in ADVISORY_PROBES]
    return advisory, required


def create_health_router(probes: dict[str, ProbeFn] | None = None) -> APIRouter:
    router = APIRouter()

    # /startupz latch: once every probe has been green at least once, stays
    # green forever. k8s startupProbe is "have we ever been ready", not "are
    # we ready now" (that's /readyz). Closing over a list keeps the bool
    # mutable from the inner closure without `nonlocal`.
    started: list[bool] = [False]

    @router.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get("/startupz")
    async def startupz() -> JSONResponse:
        if started[0]:
            return JSONResponse({"status": "ok"})
        if not probes:
            return JSONResponse(
                {"status": "starting", "reason": "probes not configured"},
                status_code=503,
            )
        _, required = await _sweep(probes)
        if required:
            return JSONResponse({"status": "starting"}, status_code=503)
        # Advisory failures do not hold the latch: a Redis that is down at boot
        # would otherwise pin the pod in "starting" for as long as it stays down.
        started[0] = True
        return JSONResponse({"status": "ok"})

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        if not probes:
            return JSONResponse(
                {"status": "degraded", "reason": "probes not configured"},
                status_code=503,
            )
        degraded, required = await _sweep(probes)
        # A required failure outranks any advisory one: the 503 must name the
        # dependency that actually stops the process from serving.
        if required:
            name, failure = required[0]
            return problem(
                503,
                error_code=failure.error_code,
                title="readiness probe failed",
                detail=f"{name}: {failure.detail}",
            )
        if degraded:
            # 200, because k8s pulls the Pod from the Service on a 503 and these
            # dependencies are ones the process is written to survive. Named in
            # the body so the degradation is still visible to operators.
            return JSONResponse({"status": "degraded", "degraded": [n for n, _ in degraded]})
        return JSONResponse({"status": "ok"})

    return router
