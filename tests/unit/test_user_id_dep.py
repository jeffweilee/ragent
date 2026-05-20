"""T8.D2 — ``get_user_id`` dep contract.

Pins:
  * Scope key takes precedence (set by ``_x_user_id_middleware``).
  * Header fallback covers unit tests that bypass the middleware.
  * Returns ``None`` when neither channel carries a value (route handlers
    can still gate locally — production middleware would 422/401 first).
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ragent.auth.deps import get_user_id
from ragent.middleware.logging import SCOPE_USER_ID_KEY


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/who")
    async def who(user_id: str | None = Depends(get_user_id)) -> dict:
        return {"user_id": user_id}

    return app


def test_returns_scope_value_when_middleware_populated_it(app: FastAPI) -> None:
    @app.middleware("http")
    async def _set_scope(request, call_next):  # type: ignore[no-untyped-def]
        request.scope[SCOPE_USER_ID_KEY] = "alice-from-jwt"
        return await call_next(request)

    with TestClient(app) as client:
        resp = client.get("/who")
        assert resp.json() == {"user_id": "alice-from-jwt"}


def test_falls_back_to_header_when_scope_empty(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.get("/who", headers={"X-User-Id": "bob"})
        assert resp.json() == {"user_id": "bob"}


def test_returns_none_when_neither_channel_set(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.get("/who")
        assert resp.json() == {"user_id": None}


def test_scope_value_wins_over_header(app: FastAPI) -> None:
    """JWT-resolved scope id must override any client-supplied header."""

    @app.middleware("http")
    async def _set_scope(request, call_next):  # type: ignore[no-untyped-def]
        request.scope[SCOPE_USER_ID_KEY] = "from-jwt"
        return await call_next(request)

    with TestClient(app) as client:
        resp = client.get("/who", headers={"X-User-Id": "client-claimed"})
        assert resp.json() == {"user_id": "from-jwt"}


def test_dep_is_not_visible_in_openapi_parameters(app: FastAPI) -> None:
    """The whole point of the dep is to keep routes free of per-route header
    declarations — the resulting OpenAPI must NOT list X-User-Id as a header
    parameter (the global security scheme from T8.D1 is the only source)."""
    op = app.openapi()["paths"]["/who"]["get"]
    params = op.get("parameters", [])
    header_params = [p for p in params if p.get("in") == "header"]
    assert header_params == []
