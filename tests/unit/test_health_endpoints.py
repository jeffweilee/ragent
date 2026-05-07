"""T7.7 (partial) — /livez and /metrics health endpoint unit tests (no Docker required)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.bootstrap.metrics import setup_metrics
from ragent.routers.health import create_health_router


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_health_router())
    setup_metrics(app)
    return TestClient(app, raise_server_exceptions=True)


def test_livez_always_200(client: TestClient) -> None:
    resp = client.get("/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_livez_no_user_id_required(client: TestClient) -> None:
    resp = client.get("/livez", headers={})
    assert resp.status_code == 200


def test_metrics_200_prometheus_format(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    content_type = resp.headers["content-type"]
    assert "text/plain" in content_type
    body = resp.text
    assert "reconciler_tick_total" in body
    assert "minio_orphan_object_total" in body
    assert "multi_ready_repaired_total" in body
    assert "worker_pipeline_duration_seconds" in body


def test_metrics_no_user_id_required(client: TestClient) -> None:
    resp = client.get("/metrics", headers={})
    assert resp.status_code == 200


def test_startupz_returns_503_until_first_probe_sweep_succeeds() -> None:
    """503 until every probe has reported green at least once.

    /livez stays trivial (no I/O) so k8s livenessProbe restarts hung pods.
    /startupz gates traffic on cold-start dependency readiness so a slow
    MinIO / ES doesn't trip /livez during boot.
    """
    from ragent.routers.health import create_health_router

    state: dict[str, bool] = {"ok": False}

    async def _ok() -> None:
        if not state["ok"]:
            raise RuntimeError("not ready yet")

    app = FastAPI()
    app.include_router(create_health_router(probes={"mariadb": _ok}))
    c = TestClient(app)

    assert c.get("/startupz").status_code == 503
    state["ok"] = True
    # First successful sweep flips the latch.
    assert c.get("/startupz").status_code == 200
    state["ok"] = False
    # Latch stays once flipped — k8s startupProbe is "have we ever been ready",
    # not "are we ready now" (that's /readyz).
    assert c.get("/startupz").status_code == 200
