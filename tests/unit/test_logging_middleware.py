"""Unit tests for ragent.middleware.logging.RequestLoggingMiddleware."""

from __future__ import annotations

import uuid

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset():
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _build_app() -> FastAPI:
    from ragent.bootstrap.logging_config import configure_logging
    from ragent.middleware.logging import RequestLoggingMiddleware

    configure_logging("ragent-test")
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/ping")
    async def ping():  # type: ignore[no-untyped-def]
        return {"ok": True}

    @app.get("/livez")
    async def livez():  # type: ignore[no-untyped-def]
        return {"status": "live"}

    @app.get("/boom")
    async def boom():  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    return app


def _captured_events():
    return structlog.testing.capture_logs()


def test_request_log_emitted_with_required_fields():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/ping", headers={"X-User-Id": "u1"})
    assert resp.status_code == 200
    api_logs = [e for e in logs if e.get("event") == "api.request"]
    assert len(api_logs) == 1
    rec = api_logs[0]
    assert rec["method"] == "GET"
    assert rec["path"] == "/ping"
    assert rec["status_code"] == 200
    assert isinstance(rec["duration_ms"], float)
    assert rec["user_id"] == "u1"
    uuid.UUID(rec["request_id"])  # valid UUID


def test_request_id_echoed_in_response_header():
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/ping")
    rid = resp.headers.get("X-Request-Id")
    assert rid is not None
    uuid.UUID(rid)


def test_incoming_request_id_honored():
    app = _build_app()
    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/ping", headers={"X-Request-Id": "abc-123"})
    assert resp.headers["X-Request-Id"] == "abc-123"
    rec = next(e for e in logs if e.get("event") == "api.request")
    assert rec["request_id"] == "abc-123"


def test_health_endpoints_not_traced():
    app = _build_app()
    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        client.get("/livez")
    assert not any(e.get("event") == "api.request" for e in logs)


def test_exception_logged_then_reraised():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/boom")
    assert resp.status_code == 500
    error_logs = [e for e in logs if e.get("event") == "api.error"]
    assert len(error_logs) == 1
    assert error_logs[0]["path"] == "/boom"


def test_invalid_request_id_replaced_with_uuid():
    app = _build_app()
    client = TestClient(app)
    bogus = "x" * 200  # too long, must be replaced
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/ping", headers={"X-Request-Id": bogus})
    rec = next(e for e in logs if e.get("event") == "api.request")
    # Either bogus is rejected and replaced, or trimmed; must be ≤128 chars and not raw bogus.
    assert len(rec["request_id"]) <= 128
    assert rec["request_id"] != bogus
    assert resp.headers["X-Request-Id"] == rec["request_id"]


def test_query_string_not_logged():
    app = _build_app()
    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        client.get("/ping?secret=abc&query=hello")
    rec = next(e for e in logs if e.get("event") == "api.request")
    assert rec["path"] == "/ping"
    assert "secret" not in repr(rec)
    assert "hello" not in repr(rec)


def test_contextvars_cleared_between_requests():
    app = _build_app()
    client = TestClient(app)
    client.get("/ping", headers={"X-Request-Id": "first"})
    # After the response, no contextvars should remain bound from this request.
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("request_id") != "first"
