"""T-CAv3.W1 — chatagent v3 integration tests (TestClient + mocked httpx)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import create_chatagent_v3_router


def _make_app():
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    app.include_router(
        create_chatagent_v3_router(
            http_client=http_mock,
            chatagent_ap_name="IntegAP",
            chatagent_auth="Bearer up",
            chatagent_api_url="http://upstream",
        )
    )
    return app, http_mock


def _resp_mock(lines: list[bytes]):
    m = MagicMock(spec=httpx.Response)
    m.raise_for_status.return_value = None
    m.iter_lines.return_value = iter([line.decode() for line in lines])
    return m


def _run_input() -> dict:
    return {
        "threadId": "thread_42",
        "runId": "run_42",
        "messages": [{"id": "m1", "role": "user", "content": "summarise the release notes"}],
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


def test_v3_full_stream_round_trip():
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(
        [
            b'{"returnCode":96200,"returnData":{"delta":"Release "}}',
            b'{"returnCode":96200,"returnData":{"delta":"notes."}}',
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
    assert "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT") == (
        "Release notes."
    )


def test_v3_upstream_timeout_emits_run_error():
    app, http_mock = _make_app()
    http_mock.send.side_effect = httpx.TimeoutException("t/o")

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    events = _events(r.text)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.CHATAGENT_TIMEOUT
    assert events[-1]["threadId"] == "thread_42"
