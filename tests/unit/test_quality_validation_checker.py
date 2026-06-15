"""T-CVQ.1 — quality validation checker unit tests."""

from __future__ import annotations

import json

from ragent.utility.quality_validation_checker import (
    check_keywords_any,
    check_no_keywords,
    check_protocol,
    check_session_messages,
    collect_text,
    parse_sse_line,
)


# ---------------------------------------------------------------------------
# parse_sse_line
# ---------------------------------------------------------------------------


def test_parse_sse_line_parses_json() -> None:
    line = 'data: {"type":"TEXT_MESSAGE_START","messageId":"m1"}'
    event = parse_sse_line(line)
    assert event == {"type": "TEXT_MESSAGE_START", "messageId": "m1"}


def test_parse_sse_line_ignores_done_sentinel() -> None:
    assert parse_sse_line("data: [Done]") is None


def test_parse_sse_line_ignores_non_data_lines() -> None:
    assert parse_sse_line("") is None
    assert parse_sse_line(": keep-alive") is None


# ---------------------------------------------------------------------------
# collect_text
# ---------------------------------------------------------------------------


def test_collect_text_concatenates_deltas() -> None:
    events = [
        {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Hello "},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "world"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
    ]
    assert collect_text(events) == "Hello world"


def test_collect_text_ignores_non_content_events() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "abc"},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]
    assert collect_text(events) == "abc"


def test_collect_text_empty() -> None:
    assert collect_text([]) == ""


# ---------------------------------------------------------------------------
# check_keywords_any
# ---------------------------------------------------------------------------


def test_check_keywords_any_found() -> None:
    assert check_keywords_any("The quick Fox", ["fox", "lion"]) == []


def test_check_keywords_any_case_insensitive() -> None:
    assert check_keywords_any("Hello World", ["WORLD"]) == []


def test_check_keywords_any_none_found() -> None:
    reasons = check_keywords_any("foo bar", ["baz", "qux"])
    assert len(reasons) == 1
    assert "baz" in reasons[0]


def test_check_keywords_any_empty_list() -> None:
    assert check_keywords_any("anything", []) == []


# ---------------------------------------------------------------------------
# check_no_keywords
# ---------------------------------------------------------------------------


def test_check_no_keywords_clean() -> None:
    assert check_no_keywords("safe content", ["danger", "forbidden"]) == []


def test_check_no_keywords_found() -> None:
    reasons = check_no_keywords("this is dangerous content", ["dangerous"])
    assert len(reasons) == 1
    assert "dangerous" in reasons[0]


# ---------------------------------------------------------------------------
# check_protocol — valid stream
# ---------------------------------------------------------------------------


def _minimal_valid() -> list[dict]:
    return [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]


def test_check_protocol_valid_no_violations() -> None:
    assert check_protocol(_minimal_valid()) == []


def test_check_protocol_missing_run_started() -> None:
    events = _minimal_valid()[1:]
    reasons = check_protocol(events)
    assert any("RUN_STARTED" in r for r in reasons)


def test_check_protocol_missing_terminal() -> None:
    events = [e for e in _minimal_valid() if e["type"] != "RUN_FINISHED"]
    reasons = check_protocol(events)
    assert any("RUN_FINISHED" in r or "RUN_ERROR" in r for r in reasons)


def test_check_protocol_run_error_is_terminal() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "RUN_ERROR", "runId": "r1", "threadId": "t1", "message": "oops", "code": "X"},
    ]
    reasons = check_protocol(events)
    # RUN_ERROR counts as terminal — "missing terminal" should NOT appear
    assert not any("missing RUN_FINISHED" in r for r in reasons)


def test_check_protocol_run_error_reported() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "RUN_ERROR", "runId": "r1", "threadId": "t1", "message": "timeout", "code": "CHATAGENT_TIMEOUT"},
    ]
    reasons = check_protocol(events)
    assert any("RUN_ERROR" in r for r in reasons)


def test_check_protocol_unpaired_text_start() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "x"},
        # no TEXT_MESSAGE_END
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]
    reasons = check_protocol(events)
    assert any("m1" in r for r in reasons)


def test_check_protocol_text_block_no_content() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]
    reasons = check_protocol(events)
    assert any("m1" in r for r in reasons)


def test_check_protocol_unpaired_tool_call() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TOOL_CALL_START", "toolCallId": "tc1", "toolCallName": "search"},
        # no TOOL_CALL_END
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]
    reasons = check_protocol(events)
    assert any("tc1" in r for r in reasons)


def test_check_protocol_no_tool_calls_expected_violation() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "r1", "threadId": "t1"},
        {"type": "TOOL_CALL_START", "toolCallId": "tc1", "toolCallName": "search"},
        {"type": "TOOL_CALL_END", "toolCallId": "tc1"},
        {"type": "TOOL_CALL_RESULT", "toolCallId": "tc1", "messageId": "m2", "content": "x"},
        {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        {"type": "RUN_FINISHED", "runId": "r1", "threadId": "t1"},
    ]
    reasons = check_protocol(events, expect_no_tool_calls=True)
    assert any("TOOL_CALL" in r for r in reasons)


def test_check_protocol_no_tool_calls_passes_when_absent() -> None:
    assert check_protocol(_minimal_valid(), expect_no_tool_calls=True) == []


# ---------------------------------------------------------------------------
# check_session_messages
# ---------------------------------------------------------------------------


def _valid_session() -> list[dict]:
    return [
        {"id": "1", "role": "user", "content": "你好"},
        {"id": "2", "role": "assistant", "content": "你好！很高興認識你。"},
    ]


def test_check_session_messages_valid() -> None:
    assert check_session_messages(_valid_session(), keywords_any=[]) == []


def test_check_session_messages_too_few() -> None:
    reasons = check_session_messages([{"id": "1", "role": "user", "content": "hi"}], keywords_any=[])
    assert any("2" in r for r in reasons)


def test_check_session_messages_wrong_first_role() -> None:
    msgs = [
        {"id": "1", "role": "assistant", "content": "hi"},
        {"id": "2", "role": "user", "content": "hello"},
    ]
    reasons = check_session_messages(msgs, keywords_any=[])
    assert any("user" in r for r in reasons)


def test_check_session_messages_hidden_leaked() -> None:
    msgs = [
        {"id": "1", "role": "user", "content": "<hidden><context>{}</context></hidden>你好"},
        {"id": "2", "role": "assistant", "content": "hi"},
    ]
    reasons = check_session_messages(msgs, keywords_any=[])
    assert any("<hidden>" in r for r in reasons)


def test_check_session_messages_keyword_match() -> None:
    msgs = _valid_session()
    msgs[1]["content"] = "您好！很高興認識您，我是 SDK 助手，安裝很簡單。"
    assert check_session_messages(msgs, keywords_any=["安裝"]) == []


def test_check_session_messages_keyword_missing() -> None:
    msgs = _valid_session()
    reasons = check_session_messages(msgs, keywords_any=["安裝", "install"])
    assert any("安裝" in r or "install" in r for r in reasons)
