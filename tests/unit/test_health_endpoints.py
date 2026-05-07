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
