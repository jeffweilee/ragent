"""Phase A — Global exception handler MUST extract error_code + http_status
from typed domain exceptions (00_rule.md §API Error Honesty).

A plain `Exception` still collapses to 500 / INTERNAL_ERROR (existing
behaviour preserved); typed exceptions surface their domain code and the
status they declare.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError
from ragent.pipelines.observability import IngestStepError


def _app_with_handler(handler_factory):
    """Build a minimal FastAPI app wired with the production handler."""
    app = FastAPI()

    # Import the production handler factory and register it.
    from ragent.bootstrap.app import _register_unhandled_exception_handler

    _register_unhandled_exception_handler(app)
    handler_factory(app)
    return app


def _client(handler_factory) -> TestClient:
    return TestClient(_app_with_handler(handler_factory), raise_server_exceptions=False)


def test_plain_exception_collapses_to_internal_error() -> None:
    def routes(app: FastAPI) -> None:
        @app.get("/boom")
        def _boom():
            raise RuntimeError("kaboom")

    resp = _client(routes).get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert body["status"] == 500


def test_ingest_step_error_preserves_domain_code() -> None:
    def routes(app: FastAPI) -> None:
        @app.get("/embedder-down")
        def _embedder():
            raise IngestStepError("embedder failed", error_code="EMBEDDER_ERROR")

    resp = _client(routes).get("/embedder-down")
    assert resp.status_code == 500  # http_status default for IngestStepError
    body = resp.json()
    assert body["error_code"] == "EMBEDDER_ERROR"


def test_upstream_service_error_returns_502_with_domain_code() -> None:
    def routes(app: FastAPI) -> None:
        @app.get("/upstream-5xx")
        def _upstream():
            raise UpstreamServiceError(
                "embedding 503", service="embedding", error_code="EMBEDDER_ERROR"
            )

    resp = _client(routes).get("/upstream-5xx")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "EMBEDDER_ERROR"


def test_upstream_timeout_error_returns_504() -> None:
    def routes(app: FastAPI) -> None:
        @app.get("/upstream-timeout")
        def _slow():
            raise UpstreamTimeoutError("llm timeout", service="llm", error_code="LLM_TIMEOUT")

    resp = _client(routes).get("/upstream-timeout")
    assert resp.status_code == 504
    body = resp.json()
    assert body["error_code"] == "LLM_TIMEOUT"


def test_handler_logs_carry_same_error_code() -> None:
    """The log record's error_code MUST equal the response body's error_code
    so an operator can correlate a 502 response with the failure log line."""
    import structlog

    def routes(app: FastAPI) -> None:
        @app.get("/upstream-fail")
        def _f():
            raise UpstreamServiceError("rerank down", service="rerank", error_code="RERANK_ERROR")

    with structlog.testing.capture_logs() as logs:
        resp = _client(routes).get("/upstream-fail")
    assert resp.json()["error_code"] == "RERANK_ERROR"
    matched = [e for e in logs if e.get("error_code") == "RERANK_ERROR"]
    assert matched, f"expected log record carrying error_code=RERANK_ERROR, got {logs}"
