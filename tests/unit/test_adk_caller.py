"""T-CAv3.3 — ragent-side concrete ADKCaller (twp-ai upstream proxy backend)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from twp_ai.schemas import RunAgentInput

from ragent.clients.adk_caller import ADKCaller
from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError
from tests.helpers import done_line as _done_line
from tests.helpers import msg_line as _msg_line
from tests.helpers import resp_mock as _resp_mock
from tests.helpers import sse_line as _sse_line


def _request(messages: list[dict] | None = None) -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread_1",
            "runId": "run_1",
            "messages": messages
            or [{"id": "m1", "role": "user", "content": "What are the features?"}],
            "tools": [],
            "state": None,
            "context": [],
            "forwardedProps": None,
        }
    )


def _make_caller(http_mock, *, user_id="alice", user_token="tok-1"):
    return ADKCaller(
        http_client=http_mock,
        api_url="http://upstream",
        ap_name="TestAP",
        auth="Bearer up",
        user_id=user_id,
        user_token=user_token,
    )


def test_stream_deltas_builds_upstream_payload() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock, user_id="bob", user_token="tok-bob")

    list(caller.stream_deltas(_request(), "ignored-model"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["metadata"]["apName"] == "TestAP"
    assert payload["metadata"]["user"] == "bob"
    assert payload["metadata"]["userToken"] == "tok-bob"
    assert payload["metadata"]["session"] == "thread_1"
    assert payload["inputData"]["message"] == "What are the features?"
    assert payload["stream"] is True


def test_stream_deltas_uses_last_user_message() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    messages = [
        {"id": "m1", "role": "user", "content": "first"},
        {"id": "m2", "role": "assistant", "content": "reply"},
        {"id": "m3", "role": "user", "content": "latest question"},
    ]

    list(caller.stream_deltas(_request(messages), "m"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"]["message"] == "latest question"


def test_stream_deltas_yields_upstream_messages_until_done() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line("The ", message_id="msg-1"),
            _msg_line("features", message_id="msg-1"),
            _done_line(),
            _msg_line("after done ignored", message_id="msg-1"),
        ]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert len(msgs) == 2
    assert msgs[0].content == "The "
    assert msgs[1].content == "features"


def test_stream_deltas_parses_agent_type() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [_msg_line("hi", message_id="m1", agent_type="planner"), _done_line()]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert msgs[0].agent_type == "planner"
    assert msgs[0].message_id == "m1"


def test_stream_deltas_parses_tool_name() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line(
                None,
                message_id="msg-tc",
                finish_reason="tool_calls",
                tool_name="search",
                tool_calls=[
                    {
                        "id": "call-abc",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            ),
            _done_line(),
        ]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert msgs[0].tool_name == "search"
    assert msgs[0].finish_reason == "tool_calls"
    assert msgs[0].tool_calls[0]["id"] == "call-abc"
    assert msgs[0].tool_calls[0]["function"]["name"] == "search"


def test_stream_deltas_parses_hitl_interrupt() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line(
                None,
                message_id="hitl-1",
                hitl={
                    "isInterrupt": True,
                    "interruptMessage": "Confirm?",
                    "interruptContent": "ctx",
                },
            ),
            _done_line(),
        ]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert msgs[0].is_interrupt is True
    assert msgs[0].interrupt_message == "Confirm?"
    assert msgs[0].interrupt_content == "ctx"


def test_stream_deltas_timeout_raises_timeout_error() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.side_effect = httpx.TimeoutException("timed out")
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamTimeoutError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_TIMEOUT


def test_stream_deltas_mid_stream_timeout_raises_timeout_error() -> None:
    def _lines():
        yield _msg_line("partial", message_id="msg-1")
        raise httpx.ReadTimeout("stalled mid-stream")

    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = _lines()
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = resp
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamTimeoutError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_TIMEOUT
    resp.close.assert_called_once()


def test_stream_deltas_request_error_raises_service_error() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.side_effect = httpx.RequestError("conn refused")
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamServiceError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR


def test_stream_deltas_non_96200_raises_service_error_with_return_message() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [_sse_line({"returnCode": 96500, "returnMessage": "quota exceeded", "returnData": {}})]
    )
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamServiceError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR
    assert "quota exceeded" in str(exc.value)


def test_stream_deltas_strips_sse_data_prefix() -> None:
    """Upstream sends 'data: {json}' lines — the prefix must be stripped."""
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [
            _msg_line("hello", message_id="m1"),  # already uses data: prefix via _sse_line
            _done_line(),
        ]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert len(msgs) == 1
    assert msgs[0].content == "hello"


def test_stream_deltas_truncated_stream_raises_service_error() -> None:
    """Stream that closes without [Done] is a truncated response — RUN_ERROR, not RUN_FINISHED."""
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [_msg_line("partial", message_id="msg-1")]  # no _done_line()
    )
    caller = _make_caller(http_mock)

    with pytest.raises(UpstreamServiceError) as exc:
        list(caller.stream_deltas(_request(), "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_UPSTREAM_ERROR
