"""Probe-level metrics inside `run_probe`.

`/readyz` aggregates probes; without per-probe metrics a failing dependency
disappears into a single 503. These metrics let dashboards alert on the
exact dep that broke and graph cold-start latency per dep independently.
"""

from __future__ import annotations

import asyncio

from prometheus_client import REGISTRY

from ragent.routers.health_probes import run_probe


def _value(metric: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(metric, labels) or 0.0


async def test_successful_probe_records_duration_and_status_ok() -> None:
    async def _ok() -> None:
        await asyncio.sleep(0)

    failure = await run_probe("mariadb", _ok)
    assert failure is None
    assert _value("ragent_readyz_probe_duration_seconds_count", probe="mariadb") >= 1
    assert _value("ragent_readyz_probe_status", probe="mariadb") == 1.0


async def test_failed_probe_increments_counter_and_flips_gauge() -> None:
    before = _value("ragent_readyz_probe_failures_total", probe="es", error_code="DEPENDENCY_DOWN")

    async def _boom() -> None:
        raise RuntimeError("nope")

    failure = await run_probe("es", _boom)
    assert failure is not None
    assert (
        _value("ragent_readyz_probe_failures_total", probe="es", error_code="DEPENDENCY_DOWN")
        == before + 1
    )
    assert _value("ragent_readyz_probe_status", probe="es") == 0.0


async def test_timeout_increments_counter_with_timeout_code(monkeypatch) -> None:
    monkeypatch.setenv("READYZ_PROBE_TIMEOUT_SECONDS", "0.01")
    before = _value("ragent_readyz_probe_failures_total", probe="redis", error_code="PROBE_TIMEOUT")

    async def _slow() -> None:
        await asyncio.sleep(1)

    failure = await run_probe("redis", _slow)
    assert failure is not None
    assert failure.error_code == "PROBE_TIMEOUT"
    assert (
        _value("ragent_readyz_probe_failures_total", probe="redis", error_code="PROBE_TIMEOUT")
        == before + 1
    )
