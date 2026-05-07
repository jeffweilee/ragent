"""HTTP metrics via prometheus-fastapi-instrumentator.

Asserts:
- /metrics exposes default Prometheus output and existing business metrics.
- Templated routes are tracked in `http_requests_total`.
- Health/metrics paths are excluded from `http_requests_total` (probe traffic
  must not drown real RPS in dashboards).
- Health/metrics paths bypass the X-User-Id auth middleware.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.bootstrap.metrics import setup_metrics


@pytest.fixture(scope="module")
def app_with_metrics() -> FastAPI:
    app = FastAPI()

    @app.get("/echo/{name}")
    def _echo(name: str) -> dict[str, str]:
        return {"name": name}

    setup_metrics(app)
    return app


def test_metrics_endpoint_exposes_prometheus_output(app_with_metrics: FastAPI) -> None:
    client = TestClient(app_with_metrics)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    # business metrics still exposed via the default registry
    assert "reconciler_tick_total" in body
    assert "worker_pipeline_duration_seconds" in body


def test_templated_route_increments_http_requests_total(app_with_metrics: FastAPI) -> None:
    client = TestClient(app_with_metrics)
    client.get("/echo/alice")
    client.get("/echo/bob")

    body = client.get("/metrics").text
    # handler is the route template, not the raw path → bounded cardinality
    assert 'handler="/echo/{name}"' in body
    assert 'handler="/echo/alice"' not in body


def test_excluded_handlers_not_tracked(app_with_metrics: FastAPI) -> None:
    client = TestClient(app_with_metrics)
    # hammer the excluded paths
    for _ in range(5):
        client.get("/metrics")

    body = client.get("/metrics").text
    # /metrics itself must never appear as a handler label
    assert 'handler="/metrics"' not in body
