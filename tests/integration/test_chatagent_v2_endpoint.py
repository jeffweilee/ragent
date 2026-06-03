"""T-CAv2.W1 — chatagent v2 integration tests (TestClient + mocked httpx)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.clients.rate_limiter import RateLimitResult
from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v2 import create_chatagent_v2_router


def _make_app(*, rate_limiter: Any = None):
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    router = create_chatagent_v2_router(
        http_client=http_mock,
        chatagent_ap_name="IntegAP",
        chatagent_api_url="http://upstream",
        rate_limiter=rate_limiter,
    )
    app.include_router(router)
    return app, http_mock


def _send_mock(raw: bytes, content_type: str = "application/json"):
    m = MagicMock(spec=httpx.Response)
    m.raise_for_status.return_value = None
    m.headers = {"content-type": content_type}
    m.content = raw
    m.iter_bytes.return_value = iter([raw])
    return m


# ── non-streaming ──────────────────────────────────────────────────────────────


def test_post_non_streaming_happy_path():
    app, http_mock = _make_app()
    raw = b'{"returnCode":96200,"returnData":{"messages":[{"content":"ok"}]}}'
    http_mock.send.return_value = _send_mock(raw)

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hello"}},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert r.content == raw


def test_post_session_auto_generated():
    app, http_mock = _make_app()
    http_mock.send.return_value = _send_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["session"]  # auto-generated, non-empty


def test_post_session_caller_supplied():
    app, http_mock = _make_app()
    http_mock.send.return_value = _send_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"metadata": {"session": "my-sess"}, "inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["session"] == "my-sess"


def test_post_flexible_input_data_forwarded():
    """Arbitrary inputData fields (e.g. messageMeta) are forwarded verbatim."""
    app, http_mock = _make_app()
    http_mock.send.return_value = _send_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi", "messageMeta": {"foo": "bar"}}},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"]["messageMeta"] == {"foo": "bar"}


def test_post_timeout_returns_504():
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.TimeoutException("t/o")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_TIMEOUT


def test_post_upstream_error_returns_502():
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.RequestError("conn refused")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


# ── streaming ─────────────────────────────────────────────────────────────────


def test_post_streaming_happy_path():
    chunks = [b'{"delta":"a"}', b'{"delta":"b"}', b'{"done":true}']
    http_mock = MagicMock(spec=httpx.Client)
    resp_mock = _send_mock(b"".join(chunks))
    resp_mock.iter_bytes.return_value = iter(chunks)
    http_mock.send.return_value = resp_mock

    app = FastAPI()
    app.include_router(
        create_chatagent_v2_router(
            http_client=http_mock,
            chatagent_ap_name="IntegAP",
            chatagent_api_url="http://upstream",
        )
    )

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "stream"}, "stream": True},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert r.content == b"".join(chunks)


def test_post_streaming_timeout_returns_504():
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.side_effect = httpx.TimeoutException("t/o")

    app = FastAPI()
    app.include_router(
        create_chatagent_v2_router(
            http_client=http_mock,
            chatagent_ap_name="IntegAP",
            chatagent_api_url="http://upstream",
        )
    )

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}, "stream": True},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_TIMEOUT


# ── rate limiting ─────────────────────────────────────────────────────────────


def test_rate_limit_returns_429():
    from ragent.clients.rate_limiter import RateLimiter

    rl = MagicMock(spec=RateLimiter)
    result = MagicMock(spec=RateLimitResult)
    result.allowed = False
    result.reset_at = 9999999999.0
    rl.check.return_value = result

    app, _ = _make_app(rate_limiter=rl)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "dave"},
        )
    assert r.status_code == 429
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_RATE_LIMITED
