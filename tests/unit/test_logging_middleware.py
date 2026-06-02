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


def test_jwt_mode_api_request_log_carries_resolved_user_id(oidc_token_manager, make_token):
    """JWT-authenticated requests emit `api.request` with the verified user_id.

    The outer ``RequestLoggingMiddleware`` runs before the inner
    ``_x_user_id_middleware`` resolves the JWT, and Starlette's per-Request
    Headers replacement prevents direct header propagation across the
    middleware boundary; the contract pinned here is that the resolved id
    still reaches the outer log via the ASGI scope dict-key channel.
    """
    from ragent.bootstrap.app import _x_user_id_middleware
    from ragent.bootstrap.logging_config import configure_logging
    from ragent.middleware.logging import RequestLoggingMiddleware

    configure_logging("ragent-test")
    app = FastAPI()
    # Order matters: register _x_user_id_middleware FIRST so it runs as the
    # innermost middleware; RequestLoggingMiddleware is registered LAST so it
    # wraps everything (Starlette wraps in reverse registration order).
    from ragent.bootstrap.auth_mode import AuthMode

    _x_user_id_middleware(
        app,
        auth_mode=AuthMode.jwt_header,
        token_manager=oidc_token_manager,
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/protected")
    async def protected():  # type: ignore[no-untyped-def]
        return {"ok": True}

    token = make_token(preferred_username="alice")
    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/protected", headers={"X-Auth-Token": token})
    assert resp.status_code == 200
    api_logs = [e for e in logs if e.get("event") == "api.request"]
    assert len(api_logs) == 1
    assert api_logs[0]["user_id"] == "alice", api_logs[0]


def test_trust_header_mode_custom_header_name_carries_user_id_in_api_request_log():
    """Trust-header mode with a non-default ``RAGENT_USER_ID_HEADER`` must
    still surface ``user_id`` on the final ``api.request`` log.

    The outer ``RequestLoggingMiddleware`` reads the inbound header using the
    canonical name ``X-User-Id`` (its own constant) — when the operator
    customises the header name, the outer read misses the value at request
    entry. The inner ``_x_user_id_middleware`` is the only layer that knows
    the configured name, so it MUST propagate the resolved id through the
    ASGI scope dict-key channel for the outer log to see it. Symmetric with
    the JWT-mode contract pinned above.
    """
    from ragent.bootstrap.app import _x_user_id_middleware
    from ragent.bootstrap.logging_config import configure_logging
    from ragent.middleware.logging import RequestLoggingMiddleware

    configure_logging("ragent-test")
    app = FastAPI()
    from ragent.bootstrap.auth_mode import AuthMode

    _x_user_id_middleware(
        app,
        auth_mode=AuthMode.user_header,
        user_id_header="X-Whoami",
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/p")
    async def p():  # type: ignore[no-untyped-def]
        return {"ok": True}

    client = TestClient(app)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/p", headers={"X-Whoami": "alice"})
    assert resp.status_code == 200
    api_logs = [e for e in logs if e.get("event") == "api.request"]
    assert len(api_logs) == 1
    assert api_logs[0].get("user_id") == "alice", api_logs[0]
