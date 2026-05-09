"""Phase D — router/middleware business logs paired with non-2xx responses
(00_rule.md §Service Boundary Logs + §API Error Honesty).

Each test verifies that a log line carrying the same ``error_code`` as the
HTTP response body fires from the rejection site, so an operator can grep
the log for the same ``error_code`` they see in the client's response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Item 11: missing X-User-Id (middleware → 422)
# ---------------------------------------------------------------------------


def test_missing_user_id_emits_business_log():
    from ragent.bootstrap.app import _x_user_id_middleware

    app = FastAPI()
    _x_user_id_middleware(app)

    @app.get("/protected")
    async def _ok():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/protected")
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MISSING_USER_ID"
    matched = [e for e in logs if e.get("error_code") == "MISSING_USER_ID"]
    assert matched, f"no log carrying MISSING_USER_ID; got events={[e['event'] for e in logs]}"


# ---------------------------------------------------------------------------
# Item 12: ingest validation rejection (415/422)
# ---------------------------------------------------------------------------


def test_ingest_validation_rejection_emits_business_log():
    from ragent.routers.ingest import create_router

    svc = MagicMock()
    app = FastAPI()
    app.include_router(create_router(svc))

    client = TestClient(app, raise_server_exceptions=False)
    # Send a body that fails Pydantic validation (missing required fields).
    with structlog.testing.capture_logs() as logs:
        resp = client.post("/ingest", json={"ingest_type": "inline"}, headers={"X-User-Id": "u"})
    assert resp.status_code in (415, 422)
    error_code = resp.json()["error_code"]
    matched = [e for e in logs if e.get("error_code") == error_code]
    assert matched, (
        f"no business log carrying error_code={error_code}; got events={[e['event'] for e in logs]}"
    )


# ---------------------------------------------------------------------------
# Item 13: GET /ingest/{id} 404
# ---------------------------------------------------------------------------


def test_ingest_not_found_emits_business_log():
    from ragent.routers.ingest import create_router

    svc = MagicMock()
    svc.get = AsyncMock(return_value=None)
    app = FastAPI()
    app.include_router(create_router(svc))

    client = TestClient(app, raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/ingest/DOES-NOT-EXIST")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "INGEST_NOT_FOUND"
    matched = [e for e in logs if e.get("error_code") == "INGEST_NOT_FOUND"]
    assert matched, f"no log carrying INGEST_NOT_FOUND; got events={[e['event'] for e in logs]}"
    notfound = matched[0]
    assert notfound.get("document_id") == "DOES-NOT-EXIST"


# ---------------------------------------------------------------------------
# Item 14: chat 429 rate limit
# ---------------------------------------------------------------------------


def test_chat_rate_limited_emits_business_log():
    from ragent.clients.rate_limiter import RateLimitResult
    from ragent.routers.chat import create_chat_router

    rate_limiter = MagicMock()
    rate_limiter.check.return_value = RateLimitResult(
        allowed=False, remaining=0, reset_at=9999999999.0
    )

    app = FastAPI()
    app.include_router(
        create_chat_router(
            retrieval_pipeline=MagicMock(),
            llm_client=MagicMock(),
            rate_limiter=rate_limiter,
            rate_limit=1,
            rate_limit_window=60,
        )
    )

    client = TestClient(app, raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/chat",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "model": "m",
                "provider": "openai",
            },
            headers={"X-User-Id": "u1"},
        )
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "CHAT_RATE_LIMITED"
    matched = [e for e in logs if e.get("error_code") == "CHAT_RATE_LIMITED"]
    assert matched, f"no log carrying CHAT_RATE_LIMITED; got events={[e['event'] for e in logs]}"
    log = matched[0]
    assert log.get("user_id") == "u1"
