"""T-CAv3.W1 — chatagent v3 integration tests (TestClient + mocked httpx)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import create_chatagent_v3_router
from tests.helpers import done_line as _done_line
from tests.helpers import msg_line as _msg_line
from tests.helpers import parse_sse_events as _events
from tests.helpers import resp_mock as _resp_mock


def _make_app():
    http_mock = MagicMock(spec=httpx.Client)
    app = FastAPI()
    app.include_router(
        create_chatagent_v3_router(
            http_client=http_mock,
            chatagent_ap_name="IntegAP",
            chatagent_auth="Bearer up",
            chatagent_api_url="http://upstream",
            chatagent_sessionlist_api_url="http://sessionlist",
            chatagent_session_api_url="http://session",
        )
    )
    return app, http_mock


def _get_ok(payload: dict) -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.raise_for_status.return_value = None
    m.json.return_value = payload
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


def test_v3_full_stream_round_trip():
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line("Release ", message_id="msg-1"),
            _msg_line("notes.", message_id="msg-1"),
            _done_line(),
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


def test_v3_session_get_maps_roles_and_strips_hidden():
    app, http_mock = _make_app()
    http_mock.get.return_value = _get_ok(
        {
            "session": "s1",
            "sessionName": "<hidden>\n<context>[]</context>\n</hidden>\n\nWhat is X?",
            "messages": [
                {
                    "messageId": "u1",
                    "role": "user",
                    "content": "<hidden>\n<context>[]</context>\n</hidden>\n\nWhat is X?",
                },
                {
                    "messageId": "p1",
                    "role": "assistant",
                    "content": "Planning...",
                    "messageMeta": {"langgraph_node": "planner"},
                },
                {
                    "messageId": "s1m",
                    "role": "assistant",
                    "content": "Answer.",
                    "messageMeta": {"langgraph_node": "summarizer"},
                },
                {"messageId": "t1", "role": "tool", "content": "tool output"},
            ],
        }
    )

    with TestClient(app) as client:
        r = client.get("/chatagent/v3/session?session=s1", headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    body = r.json()
    assert body["session"] == "s1"
    assert body["sessionName"] == "What is X?"
    assert body["messages"] == [
        {"id": "u1", "role": "user", "content": "What is X?"},
        {"id": "p1", "role": "reasoning", "content": "Planning..."},
        {"id": "s1m", "role": "assistant", "content": "Answer."},
        {"id": "t1", "role": "tool", "content": "tool output"},
    ]


def test_v3_session_list_strips_session_names():
    app, http_mock = _make_app()
    http_mock.get.return_value = _get_ok(
        {
            "sessions": [
                {"session": "s1", "sessionName": "<context>page</context>\n\nFirst chat"},
                {"session": "s2", "sessionName": "Plain"},
            ]
        }
    )

    with TestClient(app) as client:
        r = client.get("/chatagent/v3/sessionList", headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    assert [s["sessionName"] for s in r.json()["sessions"]] == ["First chat", "Plain"]


def test_v3_session_rename_forwards_payload():
    app, http_mock = _make_app()
    upstream_body = {"returnCode": 96200, "returnData": {"session": "s1", "sessionName": "new"}}
    http_mock.request.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value=upstream_body),
    )

    with TestClient(app) as client:
        r = client.put(
            "/chatagent/v3/session",
            json={"session": "s1", "sessionName": "new"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
    assert r.json() == upstream_body
    sent = http_mock.request.call_args
    assert sent.args[0] == "PUT"
    assert sent.kwargs["json"] == {
        "session": "s1",
        "sessionName": "new",
        "apName": "IntegAP",
        "user": "alice",
    }


def test_v3_session_delete_forwards_payload():
    app, http_mock = _make_app()
    upstream_body = {"returnCode": 96200, "returnData": {}}
    http_mock.request.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value=upstream_body),
    )

    with TestClient(app) as client:
        r = client.request(
            "DELETE",
            "/chatagent/v3/session",
            json={"session": "s1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
    sent = http_mock.request.call_args
    assert sent.args[0] == "DELETE"
    assert sent.kwargs["json"] == {"session": "s1", "apName": "IntegAP", "user": "alice"}
