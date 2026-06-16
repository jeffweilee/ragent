"""Unit tests for stream generator helpers in _quality_validation.py."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx

from ragent.routers._quality_validation import (
    _build_auth_headers,
    _build_summary,
    _call_session,
    _relay_events,
    _yield_run_error,
    _yield_text,
    admin_quality_validation_stream,
)
from tests.unit.conftest import make_jwt as _make_jwt


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
    session_results = [(True, [], 4), (True, [], 3)]
    summary = _build_summary(questions, stream_results, session_results, 500)
    assert "通過：2" in summary
    assert "失敗：0" in summary
    assert "✅" in summary
    assert "500 ms" in summary
    assert "Session 4 則訊息 通過" in summary
    assert "Session 3 則訊息 通過" in summary


def test_build_summary_shows_question_text() -> None:
    questions = [_q("q1", question="What is RAGent?", expect_keywords_any=["ragent"])]
    stream_results = [(True, [])]
    session_results = [(True, [], 2)]
    summary = _build_summary(questions, stream_results, session_results, 100)
    assert "What is RAGent?" in summary
    assert "期望含有：ragent" in summary
    assert "Session 2 則訊息 通過" in summary


def test_build_summary_shows_no_keywords() -> None:
    questions = [_q("q1", question="hi?", expect_no_keywords=["bad"])]
    stream_results = [(True, [])]
    session_results = [(True, [], 2)]
    summary = _build_summary(questions, stream_results, session_results, 50)
    assert "期望不含：bad" in summary


def test_build_summary_partial_fail_shows_reasons() -> None:
    questions = [_q("q1"), _q("q2")]
    stream_results = [(True, []), (False, ["keyword missing"])]
    session_results = [(True, [], 3), (True, [], 2)]
    summary = _build_summary(questions, stream_results, session_results, 100)
    assert "通過：1" in summary
    assert "失敗：1" in summary
    assert "❌" in summary
    assert "keyword missing" in summary


def test_build_summary_session_fail_shown() -> None:
    questions = [_q("q1")]
    stream_results = [(True, [])]
    session_results = [(False, ["leaked context"], 0)]
    summary = _build_summary(questions, stream_results, session_results, 200)
    assert "❌" in summary
    assert "leaked context" in summary
    assert "Session 無訊息 失敗" in summary


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
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    resp.close = MagicMock()
    return resp


def _make_session_mock(messages: list[dict]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = messages
    return resp


def test_admin_stream_happy_path_all_pass() -> None:
    token = _make_jwt({"sub": "admin-1"})
    http_client = MagicMock(spec=httpx.Client)
    http_client.build_request.return_value = MagicMock(spec=httpx.Request)
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
    http_client.build_request.return_value = MagicMock(spec=httpx.Request)
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
    http_client.build_request.return_value = MagicMock(spec=httpx.Request)
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


# ---------------------------------------------------------------------------
# _call_session — retry behaviour
# ---------------------------------------------------------------------------


def _session_resp(status: int = 200, body: object = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if status == 200:
        resp.json.return_value = body
    return resp


@patch("ragent.routers._quality_validation.time.sleep")
def test_call_session_retries_on_non_200_then_succeeds(mock_sleep: MagicMock) -> None:
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    http_client = MagicMock(spec=httpx.Client)
    http_client.get.side_effect = [_session_resp(status=502), _make_session_mock(messages)]

    result = _call_session(http_client, "http://localhost:8000", "t1", "u1", "", "X-Auth-Token")

    assert result == messages
    assert http_client.get.call_count == 2
    mock_sleep.assert_called_once_with(1.0)


@patch("ragent.routers._quality_validation.time.sleep")
def test_call_session_retries_on_empty_messages_then_succeeds(mock_sleep: MagicMock) -> None:
    messages = [{"role": "assistant", "content": "answer"}]
    http_client = MagicMock(spec=httpx.Client)
    http_client.get.side_effect = [
        _session_resp(body={"messages": []}),
        _make_session_mock(messages),
    ]

    result = _call_session(http_client, "http://localhost:8000", "t1", "u1", "", "X-Auth-Token")

    assert result == messages
    mock_sleep.assert_called_once_with(1.0)


@patch("ragent.routers._quality_validation.time.sleep")
def test_call_session_null_messages_treated_as_empty(mock_sleep: MagicMock) -> None:
    messages = [{"role": "assistant", "content": "answer"}]
    http_client = MagicMock(spec=httpx.Client)
    http_client.get.side_effect = [
        _session_resp(body={"messages": None}),
        _make_session_mock(messages),
    ]

    result = _call_session(http_client, "http://localhost:8000", "t1", "u1", "", "X-Auth-Token")

    assert result == messages
    mock_sleep.assert_called_once_with(1.0)


@patch("ragent.routers._quality_validation.time.sleep")
def test_call_session_exhausts_retries_returns_empty(mock_sleep: MagicMock) -> None:
    http_client = MagicMock(spec=httpx.Client)
    http_client.get.return_value = _session_resp(status=502)

    result = _call_session(http_client, "http://localhost:8000", "t1", "u1", "", "X-Auth-Token")

    assert result == []
    assert http_client.get.call_count == 4  # 3 retries + 1 final
    assert mock_sleep.call_args_list == [call(1.0), call(2.0), call(4.0)]


def test_call_session_first_attempt_succeeds_no_sleep() -> None:
    messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    http_client = MagicMock(spec=httpx.Client)
    http_client.get.return_value = _make_session_mock(messages)

    with patch("ragent.routers._quality_validation.time.sleep") as mock_sleep:
        result = _call_session(http_client, "http://localhost:8000", "t1", "u1", "", "X-Auth-Token")

    assert result == messages
    assert http_client.get.call_count == 1
    mock_sleep.assert_not_called()
