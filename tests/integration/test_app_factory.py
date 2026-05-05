"""T7.5f — create_app() factory: lifespan, routers, X-User-Id middleware (B30, C9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.docker


def test_create_app_boots_against_real_dependencies(dev_env) -> None:
    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/livez").status_code == 200


def test_health_endpoints_bypass_user_id_middleware(dev_env) -> None:
    """C9 — health endpoints reachable without X-User-Id."""
    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/livez", headers={}).status_code == 200
        assert client.get("/metrics", headers={}).status_code == 200


def test_protected_route_returns_422_without_user_id(dev_env) -> None:
    from ragent.bootstrap.app import create_app

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/ingest/SOME_ID")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_lifespan_runs_init_schema_once(dev_env, monkeypatch: pytest.MonkeyPatch) -> None:
    import ragent.bootstrap.app as app_mod

    calls: list[None] = []
    real = app_mod.init_schema
    monkeypatch.setattr(app_mod, "init_schema", lambda: (calls.append(None), real())[1])

    app = app_mod.create_app()
    with TestClient(app) as client:
        client.get("/livez")
    assert len(calls) == 1
