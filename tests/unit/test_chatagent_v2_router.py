"""T-CAv2.R1–R3 — chatagent v2 router unit tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v2 import create_chatagent_v2_router


def _make_app(*, rate_limiter: Any = None, chatagent_auth: str | None = None):
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    router = create_chatagent_v2_router(
        http_client=http_mock,
        chatagent_ap_name="TestAP",
        chatagent_auth=chatagent_auth,
        chatagent_api_url="http://upstream",
        rate_limiter=rate_limiter,
    )
    app.include_router(router)
    return app, http_mock


# ── non-streaming ──────────────────────────────────────────────────────────────


def test_non_streaming_forwards_upstream_bytes():
    app, http_mock = _make_app()
    raw = b'{"returnCode":96200,"returnData":{"messages":[{"content":"ok"}]}}'
    resp_mock = MagicMock()
    resp_mock.raise_for_status.return_value = None
    resp_mock.content = raw
    resp_mock.headers = {"content-type": "application/json"}
    http_mock.post.return_value = resp_mock

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hello"}},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert r.content == raw


def test_non_streaming_injects_server_fields():
    app, http_mock = _make_app()
    resp_mock = MagicMock()
    resp_mock.raise_for_status.return_value = None
    resp_mock.content = b"{}"
    resp_mock.headers = {"content-type": "application/json"}
    http_mock.post.return_value = resp_mock

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"metadata": {"session": "s1"}, "inputData": {"message": "hi"}},
            headers={"X-User-Id": "bob", "X-Auth-Token": "tok123"},
        )

    payload = http_mock.post.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "bob"
    assert payload["metadata"]["userToken"] == "tok123"
    assert payload["metadata"]["session"] == "s1"
    assert payload["stream"] is False


def test_non_streaming_auto_generates_session_when_absent():
    app, http_mock = _make_app()
    resp_mock = MagicMock()
    resp_mock.raise_for_status.return_value = None
    resp_mock.content = b"{}"
    resp_mock.headers = {"content-type": "application/json"}
    http_mock.post.return_value = resp_mock

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.post.call_args.kwargs["json"]
    assert payload["metadata"]["session"]  # non-empty auto-generated id


def test_non_streaming_timeout_returns_504():
    app, http_mock = _make_app()
    http_mock.post.side_effect = httpx.TimeoutException("timed out")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 504
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_TIMEOUT


def test_non_streaming_upstream_error_returns_502():
    app, http_mock = _make_app()
    http_mock.post.side_effect = httpx.RequestError("conn refused")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


# ── streaming ─────────────────────────────────────────────────────────────────


def _make_stream_mock(chunks: list[bytes], content_type: str = "application/json"):
    stream_resp = MagicMock()
    stream_resp.headers = {"content-type": content_type}
    stream_resp.raise_for_status.return_value = None
    stream_resp.iter_bytes.return_value = iter(chunks)

    @contextmanager
    def _stream_ctx(*args, **kwargs):
        yield stream_resp

    stream_mock = MagicMock(spec=httpx.Client)
    stream_mock.stream = _stream_ctx
    return stream_mock


def test_streaming_forwards_all_chunks():
    chunks = [b'{"delta":"part1"}', b'{"delta":"part2"}', b'{"done":true}']
    http_mock = _make_stream_mock(chunks)
    app = FastAPI()
    router = create_chatagent_v2_router(
        http_client=http_mock,
        chatagent_ap_name="TestAP",
        chatagent_api_url="http://upstream",
    )
    app.include_router(router)

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "stream me"}, "stream": True},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert r.content == b"".join(chunks)


def test_streaming_injects_server_fields():
    captured: list[dict] = []

    @contextmanager
    def _stream_ctx(*args, **kwargs):
        captured.append(kwargs.get("json") or args[1] if len(args) > 1 else kwargs["json"])
        resp = MagicMock()
        resp.headers = {"content-type": "application/json"}
        resp.raise_for_status.return_value = None
        resp.iter_bytes.return_value = iter([b"{}"])
        yield resp

    http_mock = MagicMock(spec=httpx.Client)
    http_mock.stream = _stream_ctx

    app = FastAPI()
    router = create_chatagent_v2_router(
        http_client=http_mock,
        chatagent_ap_name="TestAP",
        chatagent_api_url="http://upstream",
    )
    app.include_router(router)

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"metadata": {"session": "s2"}, "inputData": {"message": "hi"}, "stream": True},
            headers={"X-User-Id": "carol", "X-Auth-Token": "tok456"},
        )

    assert len(captured) == 1
    meta = captured[0]["metadata"]
    assert meta["apName"] == "TestAP"
    assert meta["user"] == "carol"
    assert meta["userToken"] == "tok456"
    assert meta["session"] == "s2"
    assert captured[0]["stream"] is True


# ── rate limiting ─────────────────────────────────────────────────────────────


def test_rate_limited_returns_429():
    from ragent.clients.rate_limiter import RateLimiter

    rl_mock = MagicMock(spec=RateLimiter)
    result = MagicMock()
    result.allowed = False
    result.reset_at = 9999999999.0
    rl_mock.check.return_value = result

    app, _ = _make_app(rate_limiter=rl_mock)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "dave"},
        )
    assert r.status_code == 429
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_RATE_LIMITED
