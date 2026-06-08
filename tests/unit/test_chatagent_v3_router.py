"""T-CAv3.4 — chatagent v3 router (twp-ai protocol proxy) unit tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.clients.rate_limiter import RateLimiter, RateLimitResult
from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import create_chatagent_v3_router


def _make_app(*, rate_limiter: RateLimiter | None = None):
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    router = create_chatagent_v3_router(
        http_client=http_mock,
        chatagent_ap_name="TestAP",
        chatagent_auth="Bearer up",
        chatagent_api_url="http://upstream",
        rate_limiter=rate_limiter,
    )
    app.include_router(router)
    return app, http_mock


def _resp_mock(lines: list[bytes]):
    m = MagicMock(spec=httpx.Response)
    m.raise_for_status.return_value = None
    m.iter_lines.return_value = iter([line.decode() for line in lines])
    return m


def _run_input() -> dict:
    return {
        "threadId": "thread_1",
        "runId": "run_1",
        "messages": [{"id": "m1", "role": "user", "content": "What are the features?"}],
        "tools": [],
        "state": None,
        "context": [],
        "forwardedProps": None,
    }


def _events(text: str) -> list[dict]:
    return [
        json.loads(block.removeprefix("data: ").strip())
        for block in text.strip().split("\n\n")
        if block.strip()
    ]


def test_v3_streams_twp_ai_event_lifecycle() -> None:
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(
        [
            b'{"returnCode":96200,"returnData":{"delta":"The "}}',
            b'{"returnCode":96200,"returnData":{"delta":"features"}}',
            b'{"returnCode":96200,"returnData":{"done":true}}',
        ]
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = _events(r.text)
    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert [e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"] == [
        "The ",
        "features",
    ]
    assert events[0]["runId"] == "run_1"
    assert events[0]["threadId"] == "thread_1"


def test_v3_injects_server_metadata() -> None:
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock([b'{"returnData":{"done":true}}'])

    with TestClient(app) as client:
        client.post(
            "/chatagent/v3",
            json=_run_input(),
            headers={"X-User-Id": "bob", "X-Auth-Token": "tok-bob"},
        )

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "bob"
    assert payload["metadata"]["userToken"] == "tok-bob"
    assert payload["metadata"]["session"] == "thread_1"
    assert payload["inputData"]["message"] == "What are the features?"
    assert payload["stream"] is True


def test_v3_rate_limited_emits_run_error_not_http_429() -> None:
    rl_mock = MagicMock(spec=RateLimiter)
    result = MagicMock(spec=RateLimitResult)
    result.allowed = False
    result.reset_at = 9999999999.0
    rl_mock.check.return_value = result

    app, http_mock = _make_app(rate_limiter=rl_mock)
    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "dave"})

    assert r.status_code == 200
    events = _events(r.text)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_RATE_LIMITED
    assert events[-1]["runId"] == "run_1"
    http_mock.send.assert_not_called()


def test_v3_upstream_error_emits_run_error() -> None:
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.RequestError("conn refused")

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    events = _events(r.text)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR
