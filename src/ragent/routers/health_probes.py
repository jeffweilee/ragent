"""T7.8 — Readiness probes for /readyz: MariaDB, ES, Redis, MinIO (B4, B26-B28)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import structlog

from ragent.bootstrap.metrics import (
    readyz_probe_duration_seconds,
    readyz_probe_failures_total,
    readyz_probe_status,
)
from ragent.errors.codes import ProbeErrorCode

logger = structlog.get_logger(__name__)

# Dependencies the process degrades around instead of gating on (T-RG.6).
#
# ES: retrieval quality drops and the feedback boost stops — the API still
# answers. Redis: every surface has a documented fallback (rate limiting fails
# open, the PAT cache falls through to MariaDB, chat streams drop to
# connection-bound SSE, ingest rows stay UPLOADED for the worker sweep).
#
# Gating on these inverted the design. A red probe made `/readyz` 503, and a k8s
# readinessProbe at failureThreshold 3 then pulls the Pod out of the Service
# ~15 s later — taking the whole API down over a dependency the code is written
# to survive — while `_check_infra_ready` refused to boot at all. They stay fully
# *observed* (per-probe metrics, the `/readyz` body, the existing
# ragent_readyz_probe_status alert); they just no longer gate.
#
# MariaDB and MinIO are deliberately absent: no read path works without MariaDB,
# and ingest cannot stage bytes without MinIO.
ADVISORY_PROBES = frozenset({"es", "redis_rate_limiter"})


@dataclass(frozen=True)
class ProbeFailure:
    """Captures a probe failure with the spec'd error_code."""

    error_code: str
    detail: str


def _budget() -> float:
    return float(os.environ.get("READYZ_PROBE_TIMEOUT_SECONDS", "2"))


async def _run(fn: Callable[[], Any]) -> Any:
    return await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)


def probe_mariadb(engine: Any) -> Callable[[], Awaitable[None]]:
    async def _p() -> None:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return _p


def probe_es(es_client: Any, index_names: list[str]) -> Callable[[], Awaitable[None]]:
    async def _p() -> None:
        def _check() -> None:
            health = es_client.cluster.health()
            if health.get("status") not in ("yellow", "green"):
                raise RuntimeError(f"ES cluster unhealthy: status={health.get('status')!r}")
            for name in index_names:
                if not es_client.indices.exists(index=name):
                    raise IndexMissing(name)

        await _run(_check)

    return _p


def probe_minio(minio_client: Any) -> Callable[[], Awaitable[None]]:
    async def _p() -> None:
        await _run(lambda: list(minio_client.list_buckets()))

    return _p


def probe_redis(redis_client: Any) -> Callable[[], Awaitable[None]]:
    async def _p() -> None:
        await _run(redis_client.ping)

    return _p


class IndexMissing(Exception):
    """Raised when a required ES index is absent."""


async def run_probe(name: str, probe: Callable[[], Awaitable[None]]) -> ProbeFailure | None:
    started = time.monotonic()
    failure: ProbeFailure | None
    try:
        await asyncio.wait_for(probe(), timeout=_budget())
        failure = None
    except TimeoutError:
        failure = ProbeFailure(
            error_code=ProbeErrorCode.PROBE_TIMEOUT, detail="probe exceeded budget"
        )
    except IndexMissing as exc:
        failure = ProbeFailure(error_code=ProbeErrorCode.ES_INDEX_MISSING, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        failure = ProbeFailure(error_code=ProbeErrorCode.DEPENDENCY_DOWN, detail=str(exc))

    elapsed = time.monotonic() - started
    duration_ms = round(elapsed * 1000.0, 3)
    readyz_probe_duration_seconds.labels(probe=name).observe(elapsed)
    if failure is None:
        readyz_probe_status.labels(probe=name).set(1)
        logger.info("probe.ok", probe=name, duration_ms=duration_ms)
    else:
        readyz_probe_status.labels(probe=name).set(0)
        readyz_probe_failures_total.labels(probe=name, error_code=failure.error_code).inc()
        logger.warning(
            "probe.failed",
            probe=name,
            error_code=failure.error_code,
            detail=failure.detail,
            duration_ms=duration_ms,
        )
    return failure
