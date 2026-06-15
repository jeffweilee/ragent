"""Unit tests for stream generator helpers in _quality_validation.py."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import httpx

from ragent.routers._quality_validation import (
    _build_auth_headers,
    _build_summary,
    _relay_events,
    _yield_run_error,
    _yield_text,
    admin_quality_validation_stream,
)


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"Bearer {header}.{body}.fakesig"


def _q(qid: str, **kwargs: object) -> dict:
    return {"id": qid, "label": f"Q-{qid}", "question": "hi?", **kwargs}


# ---------------------------------------------------------------------------
# _build_auth_headers
# ---------------------------------------------------------------------------


def test_build_auth_headers_no_token_omits_jwt_key() -> None:
    headers = _build_auth_headers("u1", "", "X-Auth-Token")
    assert headers["X-User-Id"] == "u1"
    assert "X-Auth-Token" not in headers


def test_build_auth_headers_with_token_sets_jwt_header() -> None:
    headers = _build_auth_headers("u1", "Bearer tok", "X-Auth-Token")
    assert headers["X-Auth-Token"] == "Bearer tok"
    assert headers["X-User-Id"] == "u1"


def test_build_auth_headers_uses_configured_jwt_header_name() -> None:
    headers = _build_auth_headers("u1", "Bearer tok", "Authorization")
    assert "Authorization" in headers
    assert "X-Auth-Token" not in headers


# ---------------------------------------------------------------------------
# _yield_text
# ---------------------------------------------------------------------------


def test_yield_text_emits_start_content_end() -> None:
    events = list(_yield_text("m1", "hello"))
    assert len(events) == 3
    assert "TEXT_MESSAGE_START" in events[0]
    assert "hello" in events[1]
    assert "TEXT_MESSAGE_END" in events[2]


# ---------------------------------------------------------------------------
# _yield_run_error
# ---------------------------------------------------------------------------


def test_yield_run_error_emits_run_started_then_error() -> None:
    events = list(_yield_run_error("run1", "t1", "oops", "ERR_CODE"))
    assert len(events) == 2
    assert "RUN_STARTED" in events[0]
    assert "RUN_ERROR" in events[1]
    assert "oops" in events[1]


# ---------------------------------------------------------------------------
# _relay_events
# ---------------------------------------------------------------------------


def test_relay_events_filters_lifecycle_types() -> None:
    all_events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]
    relayed = list(_relay_events(all_events))
    assert len(relayed) == 3
    assert all("TEXT_MESSAGE" in r for r in relayed)


def test_relay_events_filters_run_error() -> None:
    all_events = [
        {"type": "RUN_ERROR", "runId": "r1", "message": "x", "code": "y"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"},
    ]
    relayed = list(_relay_events(all_events))
    assert len(relayed) == 1
    assert "TEXT_MESSAGE_CONTENT" in relayed[0]


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


def test_build_summary_all_pass() -> None:
    questions = [_q("q1"), _q("q2")]
    stream_results = [(True, []), (True, [])]
    session_results = [(True, []), (True, [])]
    summary = _build_summary(questions, stream_results, session_results, 500)
    assert "通過：2" in summary
    assert "失敗：0" in summary
    assert "✅" in summary
    assert "500 ms" in summary


def test_build_summary_partial_fail_shows_reasons() -> None:
    questions = [_q("q1"), _q("q2")]
    stream_results = [(True, []), (False, ["keyword missing"])]
    session_results = [(True, []), (True, [])]
    summary = _build_summary(questions, stream_results, session_results, 100)
    assert "通過：1" in summary
    assert "失敗：1" in summary
    assert "❌" in summary
    assert "keyword missing" in summary


def test_build_summary_session_fail_shown() -> None:
    questions = [_q("q1")]
    stream_results = [(True, [])]
    session_results = [(False, ["leaked context"])]
    summary = _build_summary(questions, stream_results, session_results, 200)
    assert "❌" in summary
    assert "leaked context" in summary


# ---------------------------------------------------------------------------
# admin_quality_validation_stream — early exits
# ---------------------------------------------------------------------------


def test_admin_stream_forbidden_emits_run_error() -> None:
    http_client = MagicMock(spec=httpx.Client)
    events = list(
        admin_quality_validation_stream(
            questions=[_q("q1")],
            user_id="u1",
            auth_header="",
            http_client=http_client,
            base_url="http://localhost:8000",
            run_id="run1",
            thread_id="t1",
            admin_user_ids=["admin-1"],
            jwt_claim="sub",
            jwt_header="X-Auth-Token",
            has_session_endpoint=True,
        )
    )
    assert any("RUN_ERROR" in e for e in events)
    assert any("QUALITY_VALIDATION_FORBIDDEN" in e for e in events)
    http_client.send.assert_not_called()


def test_admin_stream_not_configured_emits_run_error() -> None:
    token = _make_jwt({"sub": "admin-1"})
    http_client = MagicMock(spec=httpx.Client)
    events = list(
        admin_quality_validation_stream(
            questions=[],
            user_id="admin-1",
            auth_header=token,
            http_client=http_client,
            base_url="http://localhost:8000",
            run_id="run1",
            thread_id="t1",
            admin_user_ids=["admin-1"],
            jwt_claim="sub",
            jwt_header="X-Auth-Token",
            has_session_endpoint=True,
        )
    )
    assert any("QUALITY_VALIDATION_NOT_CONFIGURED" in e for e in events)
    http_client.send.assert_not_called()


# ---------------------------------------------------------------------------
# admin_quality_validation_stream — happy path with mocked HTTP
# ---------------------------------------------------------------------------


def _make_sse_mock(delta: str, thread_id: str = "qt1") -> MagicMock:
    lines = [
        f'data: {{"type":"RUN_STARTED","runId":"r1","threadId":"{thread_id}"}}',
        'data: {"type":"TEXT_MESSAGE_START","messageId":"m1"}',
        f'data: {{"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"{delta}"}}',
        'data: {"type":"TEXT_MESSAGE_END","messageId":"m1"}',
        f'data: {{"type":"RUN_FINISHED","runId":"r1","threadId":"{thread_id}"}}',
        "data: [Done]",
    ]
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    resp.close = MagicMock()
    return resp


def _make_session_mock(messages: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = messages
    return resp


def test_admin_stream_happy_path_all_pass() -> None:
    token = _make_jwt({"sub": "admin-1"})
    http_client = MagicMock(spec=httpx.Client)
    http_client.build_request.return_value = MagicMock()
    http_client.send.return_value = _make_sse_mock("ragent is great")
    http_client.get.return_value = _make_session_mock(
        [
            {"id": "1", "role": "user", "content": "What is ragent?"},
            {"id": "2", "role": "assistant", "content": "ragent is great"},
        ]
    )

    questions = [_q("q1", expect_keywords_any=["ragent"])]
    events = list(
        admin_quality_validation_stream(
            questions=questions,
            user_id="admin-1",
            auth_header=token,
            http_client=http_client,
            base_url="http://localhost:8000",
            run_id="run1",
            thread_id="outer-t1",
            admin_user_ids=["admin-1"],
            jwt_claim="sub",
            jwt_header="X-Auth-Token",
            has_session_endpoint=True,
        )
    )
    assert "RUN_STARTED" in events[0]
    assert "RUN_FINISHED" in events[-1]
    assert any("驗收摘要" in e for e in events)
    assert any("通過：1" in e for e in events)


def test_admin_stream_no_session_endpoint_reports_not_configured() -> None:
    token = _make_jwt({"sub": "admin-1"})
    http_client = MagicMock(spec=httpx.Client)
    http_client.build_request.return_value = MagicMock()
    http_client.send.return_value = _make_sse_mock("ragent is great")

    questions = [_q("q1", expect_keywords_any=["ragent"])]
    events = list(
        admin_quality_validation_stream(
            questions=questions,
            user_id="admin-1",
            auth_header=token,
            http_client=http_client,
            base_url="http://localhost:8000",
            run_id="run1",
            thread_id="outer-t1",
            admin_user_ids=["admin-1"],
            jwt_claim="sub",
            jwt_header="X-Auth-Token",
            has_session_endpoint=False,
        )
    )
    assert "RUN_STARTED" in events[0]
    assert "RUN_FINISHED" in events[-1]
    assert any("失敗：1" in e for e in events)


def test_admin_stream_chatagent_http_error_recorded() -> None:
    token = _make_jwt({"sub": "admin-1"})
    http_client = MagicMock(spec=httpx.Client)
    http_client.build_request.return_value = MagicMock()
    http_client.send.side_effect = httpx.ConnectError("connection refused")

    questions = [_q("q1")]
    events = list(
        admin_quality_validation_stream(
            questions=questions,
            user_id="admin-1",
            auth_header=token,
            http_client=http_client,
            base_url="http://localhost:8000",
            run_id="run1",
            thread_id="outer-t1",
            admin_user_ids=["admin-1"],
            jwt_claim="sub",
            jwt_header="X-Auth-Token",
            has_session_endpoint=True,
        )
    )
    assert "RUN_STARTED" in events[0]
    assert "RUN_FINISHED" in events[-1]
    assert any("失敗" in e for e in events)
