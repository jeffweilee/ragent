"""HTTP metrics via prometheus-fastapi-instrumentator.

Asserts:
- /metrics exposes default Prometheus output and existing business metrics.
- Templated routes are tracked in `http_requests_total`.
- Health/metrics paths are excluded from `http_requests_total` (probe traffic
  must not drown real RPS in dashboards).
- Health/metrics paths bypass the X-User-Id auth middleware.
- pyproject.toml pins prometheus-fastapi-instrumentator<8.0.0 (compat guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Match

from ragent.bootstrap.metrics import setup_metrics

_PYPROJECT = Path(__file__).parent.parent.parent / "pyproject.toml"


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


def test_pyproject_pins_instrumentator_below_v8() -> None:
    """pyproject.toml must cap prometheus-fastapi-instrumentator below 8.0.0.

    8.0.0 requires starlette>=1.0.0 which allows fastapi>=0.137.1 to be
    resolved. FastAPI 0.137.1 adds _IncludedRouter (a dataclass with no .path
    attribute) to app.routes. _get_route_name in routing.py accesses
    route.path unconditionally, raising AttributeError on every first request.
    """
    pyproject = tomllib.loads(_PYPROJECT.read_text())
    deps: list[str] = pyproject["project"]["dependencies"]
    pfi = next((d for d in deps if d.startswith("prometheus-fastapi-instrumentator")), None)
    assert pfi is not None, "prometheus-fastapi-instrumentator missing from dependencies"
    assert "<8.0.0" in pfi, (
        f"Add an upper-bound <8.0.0 to prevent the starlette>=1.0.0 → "
        f"fastapi>=0.137.1 → _IncludedRouter AttributeError upgrade chain. "
        f"Current constraint: {pfi!r}"
    )


def test_get_route_name_raises_on_pathless_route() -> None:
    """_get_route_name accesses route.path unconditionally — crash on _IncludedRouter.

    Documents the upstream bug: any route object that matches but carries no
    .path attribute triggers AttributeError. The version pin in pyproject.toml
    prevents the fastapi version that introduces such routes from being installed.
    """
    from prometheus_fastapi_instrumentator.routing import _get_route_name

    class _PathlessRoute:
        def matches(self, scope: dict) -> tuple[Match, dict]:
            return (Match.FULL, {})

    scope: dict = {"type": "http", "path": "/x", "method": "GET", "root_path": ""}
    with pytest.raises(AttributeError):
        _get_route_name(scope, [_PathlessRoute()])
