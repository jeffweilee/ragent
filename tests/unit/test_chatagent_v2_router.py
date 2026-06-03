"""T-CAv2.R1–R3 — chatagent v2 router unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.clients.rate_limiter import RateLimiter, RateLimitResult
from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v2 import create_chatagent_v2_router


def _make_app(*, rate_limiter: RateLimiter | None = None, chatagent_auth: str | None = None):
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


def _resp_mock(raw: bytes, content_type: str = "application/json"):
    m = MagicMock(spec=httpx.Response)
    m.raise_for_status.return_value = None
    m.headers = {"content-type": content_type}
    m.content = raw
    m.iter_bytes.return_value = iter([raw])
    return m


# ── non-streaming ──────────────────────────────────────────────────────────────


def test_non_streaming_forwards_upstream_bytes():
    app, http_mock = _make_app()
    raw = b'{"returnCode":96200,"returnData":{"messages":[{"content":"ok"}]}}'
    http_mock.send.return_value = _resp_mock(raw)

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
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"metadata": {"session": "s1"}, "inputData": {"message": "hi"}},
            headers={"X-User-Id": "bob", "X-Auth-Token": "tok123"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "bob"
    assert payload["metadata"]["userToken"] == "tok123"
    assert payload["metadata"]["session"] == "s1"
    assert payload["stream"] is False


def test_non_streaming_auto_generates_session_when_absent():
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["session"]  # non-empty auto-generated id


def test_non_streaming_forwards_extra_input_fields():
    """Arbitrary inputData fields are forwarded verbatim to upstream."""
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi", "messageMeta": {"foo": "bar"}}},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"]["messageMeta"] == {"foo": "bar"}


def test_non_streaming_forwards_unknown_top_level_fields():
    """Unknown top-level fields are forwarded verbatim to upstream."""
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}, "customField": "value"},
            headers={"X-User-Id": "alice"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["customField"] == "value"


def test_non_streaming_server_fields_overwrite_caller_metadata():
    """Server-injected fields always win over caller-supplied metadata."""
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"metadata": {"apName": "Fake", "user": "hacker", "userToken": "stolen"}},
            headers={"X-User-Id": "alice", "X-Auth-Token": "real-tok"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "alice"
    assert payload["metadata"]["userToken"] == "real-tok"


def test_non_streaming_non_dict_metadata_is_ignored():
    """Non-dict metadata (string, list, etc.) is treated as absent — no 500."""
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"metadata": "not-a-dict"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["session"]  # auto-generated


def test_non_streaming_string_stream_false_not_treated_as_streaming():
    """stream='false' (string) must not trigger the streaming path."""
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(b"{}")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}, "stream": "false"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["stream"] is False  # coerced: "false" string → non-streaming


def test_non_streaming_timeout_returns_504():
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.TimeoutException("timed out")

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


def test_streaming_forwards_all_chunks():
    chunks = [b'{"delta":"part1"}', b'{"delta":"part2"}', b'{"done":true}']
    app, http_mock = _make_app()
    resp_mock = _resp_mock(b"".join(chunks))
    resp_mock.iter_bytes.return_value = iter(chunks)
    http_mock.send.return_value = resp_mock

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "stream me"}, "stream": True},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert r.content == b"".join(chunks)


def test_streaming_injects_server_fields():
    app, http_mock = _make_app()
    resp_mock = _resp_mock(b"{}")
    resp_mock.iter_bytes.return_value = iter([b"{}"])
    http_mock.send.return_value = resp_mock

    with TestClient(app) as client:
        client.post(
            "/chatagent/v2",
            json={"metadata": {"session": "s2"}, "inputData": {"message": "hi"}, "stream": True},
            headers={"X-User-Id": "carol", "X-Auth-Token": "tok456"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "carol"
    assert payload["metadata"]["userToken"] == "tok456"
    assert payload["metadata"]["session"] == "s2"
    assert payload["stream"] is True


def test_streaming_upstream_error_returns_502():
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.RequestError("conn refused")

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}, "stream": True},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 502
    assert r.json()["error_code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_streaming_forwards_upstream_content_type():
    app, http_mock = _make_app()
    resp_mock = _resp_mock(b"data: chunk\n\n", content_type="text/event-stream")
    resp_mock.iter_bytes.return_value = iter([b"data: chunk\n\n"])
    http_mock.send.return_value = resp_mock

    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v2",
            json={"inputData": {"message": "hi"}, "stream": True},
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]


# ── rate limiting ─────────────────────────────────────────────────────────────


def test_rate_limited_returns_429():
    rl_mock = MagicMock(spec=RateLimiter)
    result = MagicMock(spec=RateLimitResult)
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
