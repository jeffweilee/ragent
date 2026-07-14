"""T-CAv3.W1 — chatagent v3 integration tests (TestClient + mocked httpx)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.errors.codes import HttpErrorCode
from ragent.routers.chatagent_v3 import create_chatagent_v3_router
from ragent.services.attachment_ingest_service import AttachmentIngestService
from tests.helpers import done_line as _done_line
from tests.helpers import msg_line as _msg_line
from tests.helpers import parse_sse_events as _events
from tests.helpers import real_agent_factory as _real_agent_factory
from tests.helpers import resp_mock as _resp_mock


def _make_app(*, attachment_service=None):
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
            agent_factory=_real_agent_factory(
                http_mock, api_url="http://upstream", ap_name="IntegAP", auth="Bearer up"
            ),
            attachment_service=attachment_service,
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


def test_v3_interrupt_ends_run_with_interrupt_outcome():
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line(
                None,
                message_id="hitl-1",
                finish_reason="tool_calls",
                tool_calls=[{"id": "tc-1", "function": {"name": "book", "arguments": "{}"}}],
                hitl={"isInterrupt": True, "interruptMessage": "Confirm booking?"},
            ),
            _done_line(),
        ]
    )

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(), headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    events = _events(r.text)
    finished = next(e for e in events if e["type"] == "RUN_FINISHED")
    assert finished["outcome"]["type"] == "interrupt"
    interrupt = finished["outcome"]["interrupts"][0]
    assert interrupt["id"] == "hitl-1"
    assert interrupt["message"] == "Confirm booking?"
    assert interrupt["toolCallId"] == "tc-1"


def test_v3_resume_resolved_continues_upstream():
    app, http_mock = _make_app()
    http_mock.send.return_value = _resp_mock([_msg_line("done", message_id="m2"), _done_line()])
    body = {**_run_input(), "resume": [{"interruptId": "hitl-1", "status": "resolved"}]}

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"] == {"lastMessageId": "hitl-1", "message": ""}
    assert [e["type"] for e in _events(r.text)][-1] == "RUN_FINISHED"


def test_v3_resume_cancelled_finishes_without_upstream():
    app, http_mock = _make_app()
    body = {**_run_input(), "resume": [{"interruptId": "hitl-1", "status": "cancelled"}]}

    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=body, headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    http_mock.send.assert_not_called()
    events = _events(r.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert events[-1]["outcome"] == {"type": "success"}


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
                    "createTime": "2025-05-01T06:48:55.617Z",
                    "updateTime": "2025-05-01T06:49:00.000Z",
                },
                {
                    "messageId": "p1",
                    "role": "assistant",
                    "content": "Planning...",
                    "messageMeta": {"langgraph_node": "planner"},
                    "createTime": "2025-05-01T06:48:56.000Z",
                    "updateTime": "2025-05-01T06:48:56.000Z",
                },
                {
                    "messageId": "s1m",
                    "role": "assistant",
                    "content": "Answer.",
                    "messageMeta": {"langgraph_node": "summarizer"},
                    "createTime": "2025-05-01T06:48:57.000Z",
                    "updateTime": "2025-05-01T06:48:57.000Z",
                },
                # t1 carries no timestamps — exercises the null fallback path.
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
        {
            "id": "u1",
            "role": "user",
            "content": "What is X?",
            "createTime": "2025-05-01T06:48:55.617Z",
            "updateTime": "2025-05-01T06:49:00.000Z",
            "attachments": None,
        },
        {
            "id": "p1",
            "role": "reasoning",
            "content": "Planning...",
            "createTime": "2025-05-01T06:48:56.000Z",
            "updateTime": "2025-05-01T06:48:56.000Z",
            "attachments": None,
        },
        {
            "id": "s1m",
            "role": "assistant",
            "content": "Answer.",
            "createTime": "2025-05-01T06:48:57.000Z",
            "updateTime": "2025-05-01T06:48:57.000Z",
            "attachments": None,
        },
        {
            "id": "t1",
            "role": "tool",
            "content": "tool output",
            "createTime": None,
            "updateTime": None,
            "attachments": None,
        },
    ]


def test_v3_session_get_drops_interrupt_turns():
    app, http_mock = _make_app()
    http_mock.get.return_value = _get_ok(
        {
            "session": "s1",
            "sessionName": "chat",
            "messages": [
                {"messageId": "u1", "role": "user", "content": "delete all"},
                {
                    "messageId": "hitl-1",
                    "role": "assistant",
                    "content": "Confirm?",
                    "humanInTheLoopMeta": {"isInterrupt": True, "interruptMessage": "Confirm?"},
                },
                {"messageId": "a1", "role": "assistant", "content": "Done."},
            ],
        }
    )

    with TestClient(app) as client:
        r = client.get("/chatagent/v3/session?session=s1", headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["messages"]] == ["u1", "a1"]


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


def test_v3_session_list_forwards_project_scope():
    # A project-scoped list must pass `project` through to the store, or every
    # project shows the full unscoped conversation list.
    app, http_mock = _make_app()
    http_mock.get.return_value = _get_ok({"sessions": []})

    with TestClient(app) as client:
        r = client.get(
            "/chatagent/v3/sessionList",
            params={"project": "proj-123"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
    assert http_mock.get.call_args.kwargs["params"]["project"] == "proj-123"


def test_v3_session_list_omits_project_when_unscoped():
    # The global recent list has no project and must not send an empty one.
    app, http_mock = _make_app()
    http_mock.get.return_value = _get_ok({"sessions": []})

    with TestClient(app) as client:
        r = client.get("/chatagent/v3/sessionList", headers={"X-User-Id": "alice"})

    assert r.status_code == 200
    assert "project" not in http_mock.get.call_args.kwargs["params"]


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


def test_v3_session_delete_cascades_to_attachments():
    """A successful upstream session delete also cascades local attachments."""
    svc = AsyncMock(spec=AttachmentIngestService)
    app, http_mock = _make_app(attachment_service=svc)
    http_mock.request.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"returnCode": 96200, "returnData": {}}),
    )

    with TestClient(app) as client:
        r = client.request(
            "DELETE",
            "/chatagent/v3/session",
            json={"session": "s1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
    svc.delete_by_session.assert_awaited_once_with("s1")


def test_v3_session_delete_skips_cascade_when_upstream_fails():
    """A failed upstream delete must not trigger local attachment cleanup."""
    svc = AsyncMock(spec=AttachmentIngestService)
    app, http_mock = _make_app(attachment_service=svc)
    http_mock.request.side_effect = httpx.TimeoutException("slow")

    with TestClient(app) as client:
        r = client.request(
            "DELETE",
            "/chatagent/v3/session",
            json={"session": "s1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 504
    svc.delete_by_session.assert_not_called()


def test_v3_session_delete_cascade_failure_does_not_mask_upstream_response():
    """An exception from delete_by_session is fail-soft: the upstream 200 still returns."""
    svc = AsyncMock(spec=AttachmentIngestService)
    svc.delete_by_session.side_effect = RuntimeError("db down")
    app, http_mock = _make_app(attachment_service=svc)
    http_mock.request.return_value = MagicMock(
        raise_for_status=MagicMock(return_value=None),
        json=MagicMock(return_value={"returnCode": 96200, "returnData": {}}),
    )

    with TestClient(app) as client:
        r = client.request(
            "DELETE",
            "/chatagent/v3/session",
            json={"session": "s1"},
            headers={"X-User-Id": "alice"},
        )

    assert r.status_code == 200
