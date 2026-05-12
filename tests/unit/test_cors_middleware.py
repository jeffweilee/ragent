"""CORS allow-list middleware wired from CORS_ALLOW_ORIGINS env var."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from ragent.bootstrap.app import _add_cors_middleware, _x_user_id_middleware


def _build_app(origins: list[str]) -> FastAPI:
    app = FastAPI()
    _add_cors_middleware(app, origins)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    return app


def test_cors_headers_present_for_listed_origin() -> None:
    app = _build_app(["https://example.com"])
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.options(
            "/ping",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "https://example.com"


def test_cors_headers_absent_for_unlisted_origin() -> None:
    app = _build_app(["https://example.com"])
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.options(
            "/ping",
            headers={
                "Origin": "https://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") is None


def test_no_cors_middleware_when_origins_empty() -> None:
    app = _build_app([])
    middleware_classes = [m.cls for m in app.user_middleware if hasattr(m, "cls")]
    assert CORSMiddleware not in middleware_classes


def test_multiple_origins_all_allowed() -> None:
    app = _build_app(["https://a.com", "https://b.com"])
    with TestClient(app, raise_server_exceptions=True) as client:
        for origin in ("https://a.com", "https://b.com"):
            resp = client.options(
                "/ping",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
            )
            assert resp.headers.get("access-control-allow-origin") == origin


def test_list_env_parses_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.com, https://b.com ,https://c.com")
    from ragent.utility.env import list_env

    assert list_env("CORS_ALLOW_ORIGINS") == ["https://a.com", "https://b.com", "https://c.com"]


def test_list_env_returns_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    from ragent.utility.env import list_env

    assert list_env("CORS_ALLOW_ORIGINS") == []


def test_list_env_returns_empty_for_blank_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "  ")
    from ragent.utility.env import list_env

    assert list_env("CORS_ALLOW_ORIGINS") == []


def test_preflight_succeeds_without_x_user_id() -> None:
    """OPTIONS preflight must not be blocked by the X-User-Id gate."""
    app = FastAPI()
    _x_user_id_middleware(app)
    _add_cors_middleware(app, ["https://example.com"])

    @app.get("/data")
    def data() -> dict:
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.options(
            "/data",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://example.com"
