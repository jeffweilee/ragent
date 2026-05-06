"""X-User-Id middleware bypass for probe and Swagger docs endpoints (C9)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.bootstrap.app import _NO_USER_ID_PATHS, _x_user_id_middleware


def _build_app() -> FastAPI:
    app = FastAPI()
    _x_user_id_middleware(app)

    @app.get("/livez")
    def livez() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    def metrics() -> str:
        return "metrics"

    @app.get("/protected")
    def protected() -> dict:
        return {"ok": True}

    return app


@pytest.mark.parametrize("path", ["/livez", "/readyz", "/metrics"])
def test_probe_paths_bypass_user_id(path: str) -> None:
    with TestClient(_build_app()) as client:
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_swagger_doc_paths_bypass_user_id(path: str) -> None:
    with TestClient(_build_app()) as client:
        assert client.get(path).status_code == 200


def test_protected_path_requires_user_id() -> None:
    with TestClient(_build_app()) as client:
        resp = client.get("/protected")
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "MISSING_USER_ID"


def test_no_user_id_paths_includes_docs_and_probes() -> None:
    expected = {"/livez", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"}
    assert expected <= _NO_USER_ID_PATHS
