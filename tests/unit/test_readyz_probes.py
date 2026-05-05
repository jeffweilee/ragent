"""T7.7 — /readyz probe orchestration: probe failure → 503 problem+json (B4, B26-B28)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.health import create_health_router
from ragent.routers.health_probes import IndexMissing


def _client(probes: dict) -> TestClient:
    app = FastAPI()
    app.include_router(create_health_router(probes=probes))
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# All probes pass → 200
# ---------------------------------------------------------------------------


def test_readyz_all_probes_pass() -> None:
    probes = {
        "mariadb": AsyncMock(return_value=None),
        "es": AsyncMock(return_value=None),
        "minio": AsyncMock(return_value=None),
        "redis_rate_limiter": AsyncMock(return_value=None),
    }
    resp = _client(probes).get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Single probe failure → 503 with problem+json
# ---------------------------------------------------------------------------


def test_readyz_mariadb_down_returns_503() -> None:
    failing = AsyncMock(side_effect=RuntimeError("connection refused"))
    probes = {
        "mariadb": failing,
        "es": AsyncMock(return_value=None),
        "minio": AsyncMock(return_value=None),
    }
    resp = _client(probes).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "DEPENDENCY_DOWN"
    assert "mariadb" in body["detail"]


def test_readyz_es_index_missing_emits_es_index_missing_code() -> None:
    failing = AsyncMock(side_effect=IndexMissing("chunks_v1"))
    probes = {
        "es": failing,
    }
    resp = _client(probes).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "ES_INDEX_MISSING"


def test_readyz_probe_timeout_emits_probe_timeout_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def slow() -> None:
        await asyncio.sleep(10)

    monkeypatch.setenv("READYZ_PROBE_TIMEOUT_SECONDS", "0.05")
    probes = {"mariadb": slow}
    resp = _client(probes).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "PROBE_TIMEOUT"


# ---------------------------------------------------------------------------
# No probes wired → 503 degraded (initial guard)
# ---------------------------------------------------------------------------


def test_readyz_no_probes_returns_503_degraded() -> None:
    app = FastAPI()
    app.include_router(create_health_router())  # no probes
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


# ---------------------------------------------------------------------------
# Auth bypass: no X-User-Id needed (C9)
# ---------------------------------------------------------------------------


def test_readyz_no_user_id_required() -> None:
    probes = {"mariadb": AsyncMock(return_value=None)}
    resp = _client(probes).get("/readyz", headers={})
    assert resp.status_code == 200
