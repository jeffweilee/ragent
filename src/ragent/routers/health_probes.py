"""T7.8 — Readiness probes for /readyz: MariaDB, ES, Redis, MinIO (B4, B26-B28)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio

from ragent.bootstrap.metrics import (
    readyz_probe_duration_seconds,
    readyz_probe_failures_total,
    readyz_probe_status,
)
from ragent.errors.codes import ProbeErrorCode


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

    readyz_probe_duration_seconds.labels(probe=name).observe(time.monotonic() - started)
    if failure is None:
        readyz_probe_status.labels(probe=name).set(1)
    else:
        readyz_probe_status.labels(probe=name).set(0)
        readyz_probe_failures_total.labels(probe=name, error_code=failure.error_code).inc()
    return failure
