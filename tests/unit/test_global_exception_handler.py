"""Phase A — Global exception handler MUST extract error_code + http_status
from typed domain exceptions (00_rule.md §API Error Honesty).

A plain `Exception` still collapses to 500 / INTERNAL_ERROR (existing
behaviour preserved); typed exceptions surface their domain code and the
status they declare.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
import structlog
from fastapi import FastAPI
from starlette.requests import Request

from ragent.bootstrap.app import _register_unhandled_exception_handler
from ragent.errors.codes import HttpErrorCode, TaskErrorCode
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError
from ragent.pipelines.observability import IngestStepError

# Build once: extract the registered async handler for direct invocation.
# Avoids TestClient + background-thread overhead (~0.6 s/call in full suite).
_handler_app = FastAPI()
_register_unhandled_exception_handler(_handler_app)
_handler = _handler_app.exception_handlers[Exception]

_mock_request = MagicMock(spec=Request)
_mock_request.url.path = "/test"
_mock_request.method = "GET"


def _call_handler(exc: Exception) -> tuple[int, dict]:
    response = asyncio.run(_handler(_mock_request, exc))
    return response.status_code, json.loads(response.body)


@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_code"),
    [
        pytest.param(
            RuntimeError("kaboom"),
            500,
            HttpErrorCode.INTERNAL_ERROR,
            id="plain-exception-collapses-to-500",
        ),
        pytest.param(
            # IngestStepError surfaces a TaskErrorCode if it ever leaks to
            # the HTTP layer (rare — usually caught by worker), but the
            # global handler treats it the same: getattr(exc, error_code).
            IngestStepError("embedder failed", error_code=TaskErrorCode.EMBEDDER_ERROR),
            500,
            "EMBEDDER_ERROR",
            id="ingest-step-error-preserves-domain-code",
        ),
        pytest.param(
            UpstreamServiceError(
                "embedding 503", service="embedding", error_code=HttpErrorCode.EMBEDDER_ERROR
            ),
            502,
            HttpErrorCode.EMBEDDER_ERROR,
            id="upstream-service-error-502",
        ),
        pytest.param(
            UpstreamTimeoutError(
                "llm timeout", service="llm", error_code=HttpErrorCode.LLM_TIMEOUT
            ),
            504,
            HttpErrorCode.LLM_TIMEOUT,
            id="upstream-timeout-error-504",
        ),
    ],
)
def test_handler_routes_by_exception_attrs(
    exc: Exception, expected_status: int, expected_code: str
) -> None:
    status, body = _call_handler(exc)
    assert status == expected_status
    assert body["status"] == expected_status
    assert body["error_code"] == expected_code


def test_handler_logs_carry_same_error_code() -> None:
    """The log record's error_code MUST equal the response body's error_code
    so an operator can correlate a 502 response with the failure log line."""
    exc = UpstreamServiceError(
        "rerank down", service="rerank", error_code=HttpErrorCode.RERANK_ERROR
    )
    with structlog.testing.capture_logs() as logs:
        _, body = _call_handler(exc)
    assert body["error_code"] == HttpErrorCode.RERANK_ERROR
    matched = [e for e in logs if e.get("error_code") == HttpErrorCode.RERANK_ERROR]
    assert matched, f"expected log record carrying error_code=RERANK_ERROR, got {logs}"
