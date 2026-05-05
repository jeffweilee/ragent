"""T7.8 — Readiness probes for /readyz: MariaDB, ES, Redis, MinIO (B4, B26-B28)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio


@dataclass(frozen=True)
class ProbeFailure:
    """Captures a probe failure with the spec'd error_code."""

    error_code: str
    detail: str


def _budget() -> float:
    return float(os.environ.get("READYZ_PROBE_TIMEOUT_SECONDS", "2"))


async def _run(fn: Callable[[], Any]) -> Any:
    return await anyio.to_thread.run_sync(fn)


def probe_mariadb(engine: Any) -> Callable[[], Awaitable[None]]:
    async def _p() -> None:
        from sqlalchemy import text

        def _ping() -> None:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        await _run(_ping)

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
        await _run(lambda: list(minio_client._client.list_buckets()))  # noqa: SLF001

    return _p


def probe_redis(redis_client: Any) -> Callable[[], Awaitable[None]]:
    async def _p() -> None:
        await _run(redis_client.ping)

    return _p


class IndexMissing(Exception):
    """Raised when a required ES index is absent."""


async def run_probe(probe: Callable[[], Awaitable[None]]) -> ProbeFailure | None:
    try:
        await asyncio.wait_for(probe(), timeout=_budget())
    except TimeoutError:
        return ProbeFailure(error_code="PROBE_TIMEOUT", detail="probe exceeded budget")
    except IndexMissing as exc:
        return ProbeFailure(error_code="ES_INDEX_MISSING", detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        return ProbeFailure(error_code="DEPENDENCY_DOWN", detail=str(exc))
    return None
