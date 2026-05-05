"""T3.15 — POST /chat rate limiting: 429 on N+1 calls per window (B31, S37)."""

from unittest.mock import MagicMock

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.clients.rate_limiter import RateLimiter
from ragent.routers.chat import create_chat_router

pytestmark = pytest.mark.docker


def _make_app(limit: int = 2, window_seconds: int = 60):
    fake_redis = fakeredis.FakeRedis()
    rate_limiter = RateLimiter(redis_client=fake_redis)

    retrieval_pipeline = MagicMock()
    retrieval_pipeline.run.return_value = {"excerpt_truncator": {"documents": []}}
    llm_client = MagicMock()
    llm_client.chat.return_value = {"content": "ok", "usage": {}}
    llm_client.stream.return_value = iter(["ok"])

    app = FastAPI()
    router = create_chat_router(
        retrieval_pipeline=retrieval_pipeline,
        llm_client=llm_client,
        rate_limiter=rate_limiter,
        rate_limit=limit,
        rate_limit_window=window_seconds,
    )
    app.include_router(router)
    return app, fake_redis


def test_requests_under_limit_succeed():
    app, _ = _make_app(limit=3)
    with TestClient(app) as client:
        for _ in range(3):
            resp = client.post(
                "/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"X-User-Id": "alice"},
            )
            assert resp.status_code == 200


def test_request_over_limit_returns_429():
    app, _ = _make_app(limit=2)
    with TestClient(app) as client:
        for _ in range(2):
            client.post(
                "/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"X-User-Id": "alice"},
            )
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["error_code"] == "CHAT_RATE_LIMITED"
    assert "Retry-After" in resp.headers


def test_different_users_have_independent_budgets():
    app, _ = _make_app(limit=1)
    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "bob"},
        )
    assert resp.status_code == 200


def test_stream_also_rate_limited():
    app, _ = _make_app(limit=1)
    with TestClient(app) as client:
        client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
        resp = client.post(
            "/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 429


def test_window_reset_allows_new_requests():
    app, fake_redis = _make_app(limit=1)
    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
        # Simulate window expiry by flushing the rate limit key
        fake_redis.flushall()
        resp = client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert resp.status_code == 200
