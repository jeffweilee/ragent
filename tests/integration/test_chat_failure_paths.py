"""Phase G — chat failure paths (audit item 17).

Covers the upstream-failure surfaces a downstream API consumer can hit:

- LLM 5xx / timeout / empty content during /chat
- Embedder failure during /chat retrieval
- Rerank failure during /chat retrieval
- LLM mid-stream error during /chat/stream
- Rate-limit precedence — 429 fires before any upstream call

All upstream failures raise the typed exceptions added in Phase A
(UpstreamServiceError → 502, UpstreamTimeoutError → 504); the global
exception handler (Phase A) extracts error_code + http_status and
returns RFC 9457 problem-details. Phase D's router-side log_pair
ensures every non-2xx response has a paired log line carrying the
same error_code.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.bootstrap.app import _register_unhandled_exception_handler
from ragent.clients.rate_limiter import RateLimitResult
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError
from ragent.routers.chat import create_chat_router

# No pytest.mark.docker — every dependency (LLM, embedder, rate-limiter,
# retrieval pipeline) is mocked. The test exercises FastAPI + the production
# global handler in-process, so it must NOT be gated on Docker availability
# (would silently skip on Docker-less CI per tests/conftest.py
# `pytest_collection_modifyitems`).


def _build_app(*, llm_client=None, retrieval_pipeline=None, rate_limiter=None):
    """Standalone FastAPI app that wires the production global handler.

    Without _register_unhandled_exception_handler, raised typed
    exceptions would surface as 500 instead of 502/504 — the test
    needs the same handler the production app installs.
    """
    app = FastAPI()
    _register_unhandled_exception_handler(app)
    app.include_router(
        create_chat_router(
            retrieval_pipeline=retrieval_pipeline or MagicMock(),
            llm_client=llm_client or MagicMock(),
            rate_limiter=rate_limiter,
            rate_limit=60,
            rate_limit_window=60,
        )
    )
    return app


def _ok_retrieval():
    rp = MagicMock()
    rp.run.return_value = {"excerpt_truncator": {"documents": []}}
    return rp


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _post_chat(client: TestClient, **over):
    body = {
        "messages": [{"role": "user", "content": over.pop("user_msg", "hi")}],
        "model": "gptoss-120b",
        "provider": "openai",
    }
    body.update(over)
    return client.post("/chat", json=body, headers={"X-User-Id": "u1"})


# ---------------------------------------------------------------------------
# 1-3. Upstream-service 5xx variants → 502 + service-specific error_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failing_dep", "exc", "expected_code"),
    [
        pytest.param(
            "llm",
            UpstreamServiceError(
                "llm chat failed after retries: HTTP 503",
                service="llm",
                error_code="LLM_ERROR",
            ),
            "LLM_ERROR",
            id="llm-5xx",
        ),
        pytest.param(
            "retrieval",
            UpstreamServiceError(
                "embedding failed after retries: HTTP 503",
                service="embedding",
                error_code="EMBEDDER_ERROR",
            ),
            "EMBEDDER_ERROR",
            id="embedder-5xx",
        ),
        pytest.param(
            "retrieval",
            UpstreamServiceError(
                "rerank failed after retries: connection reset",
                service="rerank",
                error_code="RERANK_ERROR",
            ),
            "RERANK_ERROR",
            id="rerank-5xx",
        ),
    ],
)
def test_chat_returns_502_on_upstream_service_error(failing_dep, exc, expected_code):
    """Any UpstreamServiceError — whether raised by the LLM, embedder, or
    reranker — surfaces as 502 with the originating service's error_code."""
    llm = MagicMock()
    rp = _ok_retrieval()
    if failing_dep == "llm":
        llm.chat.side_effect = exc
    else:
        rp.run.side_effect = exc

    app = _build_app(llm_client=llm, retrieval_pipeline=rp)
    with structlog.testing.capture_logs() as logs:
        resp = _post_chat(_client(app))
    assert resp.status_code == 502
    assert resp.json()["error_code"] == expected_code
    assert any(e.get("error_code") == expected_code for e in logs), (
        f"no log carried {expected_code}; events={[e['event'] for e in logs]}"
    )


# ---------------------------------------------------------------------------
# 4. LLM timeout → 504 / LLM_TIMEOUT (separate: 504, not 502)
# ---------------------------------------------------------------------------


def test_chat_returns_504_when_llm_times_out():
    llm = MagicMock()
    llm.chat.side_effect = UpstreamTimeoutError(
        "llm chat failed after retries: read timeout", service="llm", error_code="LLM_TIMEOUT"
    )
    app = _build_app(llm_client=llm, retrieval_pipeline=_ok_retrieval())
    resp = _post_chat(_client(app))
    assert resp.status_code == 504
    assert resp.json()["error_code"] == "LLM_TIMEOUT"


# ---------------------------------------------------------------------------
# 5. /chat/stream — LLM error mid-stream → SSE error event (200 OK)
# ---------------------------------------------------------------------------


def test_chat_stream_emits_error_event_on_llm_failure():
    llm = MagicMock()
    llm.stream.side_effect = UpstreamServiceError(
        "llm stream failed after retries: HTTP 503",
        service="llm",
        error_code="LLM_ERROR",
    )
    app = _build_app(llm_client=llm, retrieval_pipeline=_ok_retrieval())
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "model": "m",
                "provider": "openai",
            },
            headers={"X-User-Id": "u1"},
        )
    # Stream endpoint absorbs upstream errors into the SSE channel,
    # so the HTTP status is still 200 — the error lives in the frame.
    assert resp.status_code == 200
    events = []
    for line in resp.text.strip().splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events, f"expected SSE error event; got {events}"


# ---------------------------------------------------------------------------
# 6. Rate-limit precedence — 429 fires before any upstream call
# ---------------------------------------------------------------------------


def test_chat_rate_limit_precedes_upstream_failure():
    """Rate limit must short-circuit BEFORE the embedder/LLM is invoked,
    even when those clients would themselves fail. A caller that's over
    quota should see CHAT_RATE_LIMITED, not the upstream error."""
    rate_limiter = MagicMock()
    rate_limiter.check.return_value = RateLimitResult(
        allowed=False, remaining=0, reset_at=9999999999.0
    )
    # Both upstreams configured to fail — they MUST NOT be reached.
    llm = MagicMock()
    llm.chat.side_effect = UpstreamServiceError(
        "should not be called", service="llm", error_code="LLM_ERROR"
    )
    rp = MagicMock()
    rp.run.side_effect = UpstreamServiceError(
        "should not be called", service="embedding", error_code="EMBEDDER_ERROR"
    )

    app = _build_app(llm_client=llm, retrieval_pipeline=rp, rate_limiter=rate_limiter)
    resp = _post_chat(_client(app))
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "CHAT_RATE_LIMITED"
    rp.run.assert_not_called()
    llm.chat.assert_not_called()


# ---------------------------------------------------------------------------
# 7. /chat/stream rate-limit precedence
# ---------------------------------------------------------------------------


def test_chat_stream_rate_limit_precedes_upstream():
    rate_limiter = MagicMock()
    rate_limiter.check.return_value = RateLimitResult(
        allowed=False, remaining=0, reset_at=9999999999.0
    )
    llm = MagicMock()
    llm.stream.side_effect = UpstreamServiceError("boom", service="llm", error_code="LLM_ERROR")
    app = _build_app(llm_client=llm, retrieval_pipeline=_ok_retrieval(), rate_limiter=rate_limiter)
    resp = _client(app).post(
        "/chat/stream",
        json={"messages": [{"role": "user", "content": "x"}], "model": "m", "provider": "openai"},
        headers={"X-User-Id": "u1"},
    )
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "CHAT_RATE_LIMITED"
    llm.stream.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Concurrent failure types — first one to fire wins
# ---------------------------------------------------------------------------


def test_chat_embedder_fails_before_llm_called():
    """When the retrieval pipeline raises, the LLM client is never invoked
    — verify the failure surface is the retrieval error, not a downstream
    LLM error."""
    llm = MagicMock()
    llm.chat.side_effect = AssertionError("LLM should not be called when retrieval fails")
    rp = MagicMock()
    rp.run.side_effect = UpstreamTimeoutError(
        "embedding timeout", service="embedding", error_code="EMBEDDER_TIMEOUT"
    )
    app = _build_app(llm_client=llm, retrieval_pipeline=rp)
    resp = _post_chat(_client(app))
    assert resp.status_code == 504
    assert resp.json()["error_code"] == "EMBEDDER_TIMEOUT"
    llm.chat.assert_not_called()
