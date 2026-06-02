"""T-CA.I1–I5 — chatagent integration tests (TestClient + mocked httpx)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent import create_chatagent_router


def _make_app(*, chatagent_auth: str | None = None, rate_limiter: Any = None):
    http_mock = MagicMock()
    app = FastAPI()
    router = create_chatagent_router(
        http_client=http_mock,
        chatagent_ap_name="IntegrationAP",
        chatagent_auth=chatagent_auth,
        chatagent_api_url="http://chatagent",
        chatagent_sessionlist_api_url="http://sessionlist",
        chatagent_session_api_url="http://session",
        rate_limiter=rate_limiter,
    )
    app.include_router(router)
    return app, http_mock


def _post_ok(content: str = "Integration answer") -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {
        "returnCode": 96200,
        "returnData": {"messages": [{"role": "assistant", "content": content, "message_id": "m1"}]},
    }
    return r


def test_chatagent_post_happy_path_200():
    app, http_mock = _make_app()
    http_mock.post.return_value = _post_ok()
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "Integration answer"
    assert body["sources"] is None
    assert body["usage"]["promptTokens"] is None


def test_chatagent_session_list_happy_path_200():
    app, http_mock = _make_app()
    http_mock.get.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(
            return_value={
                "totalCount": 1,
                "sessions": [{"session": "s1", "apName": "IntegrationAP"}],
            }
        ),
    )
    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v1/sessionList?startTime=2025-01-01T00:00:00.000Z",
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert r.json()["totalCount"] == 1


def test_chatagent_session_happy_path_200():
    app, http_mock = _make_app()
    detail = {
        "_id": "x",
        "apName": "IntegrationAP",
        "user": "alice",
        "session": "s1",
        "sessionName": "chat",
        "sessionStatus": "active",
        "messages": [],
        "createTime": "2025-05-01T00:00:00.000Z",
        "updateTime": "2025-05-01T00:00:00.000Z",
    }
    http_mock.get.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value=detail),
    )
    with TestClient(app) as client:
        r = client.get("/chatagent/v1/session?session=s1", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert r.json()["session"] == "s1"


def test_chatagent_post_rate_limited_429():
    from ragent.clients.rate_limiter import RateLimitResult

    rate_limiter = MagicMock()
    rate_limiter.check.return_value = RateLimitResult(allowed=False, remaining=0, reset_at=9999.0)
    app, http_mock = _make_app(rate_limiter=rate_limiter)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 429
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_RATE_LIMITED
    http_mock.post.assert_not_called()


def test_chatagent_post_upstream_502():
    app, http_mock = _make_app()
    http_mock.post.return_value = MagicMock(
        raise_for_status=MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
        )
    )
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v1",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR
