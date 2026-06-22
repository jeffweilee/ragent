"""T-CAv3.3 — ragent-side concrete ADKCaller (twp-ai upstream proxy backend)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from twp_ai.schemas import RunAgentInput

from ragent.clients.adk_caller import ADKCaller, ResumeValidationError
from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError
from tests.helpers import done_line as _done_line
from tests.helpers import msg_line as _msg_line
from tests.helpers import resp_mock as _resp_mock
from tests.helpers import sse_line as _sse_line


def _request(
    messages: list[dict] | None = None,
    *,
    tools: list[dict] | None = None,
    state: object = None,
    context: list[dict] | None = None,
    resume: list[dict] | None = None,
) -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread_1",
            "runId": "run_1",
            "messages": messages
            or [{"id": "m1", "role": "user", "content": "What are the features?"}],
            "tools": tools or [],
            "state": state,
            "context": context or [],
            "forwardedProps": None,
            "resume": resume,
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


def test_stream_deltas_prepends_context_and_state() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(
        tools=[{"name": "save_form", "description": "Persist the form", "parameters": {}}],
        state={"draft": "v1"},
        context=[{"description": "current page", "value": "checkout"}],
    )

    list(caller.stream_deltas(request, "m"))

    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    # Context/state are wrapped in a <hidden> block the frontend strips from history.
    assert message.startswith("<hidden>\n")
    assert '<context>[{"description": "current page", "value": "checkout"}]</context>' in message
    assert '<state>{"draft": "v1"}</state>' in message
    assert "</hidden>" in message
    # Tools are deliberately not folded into the upstream message.
    assert "save_form" not in message
    # The user's actual question stays at the end, after the folded context.
    assert message.endswith("What are the features?")


def test_stream_deltas_prepends_context_only() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(context=[{"description": "current page", "value": "checkout"}])

    list(caller.stream_deltas(request, "m"))

    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    assert message.startswith("<hidden>\n<context>")
    assert "<state>" not in message
    assert message.endswith("What are the features?")


def test_stream_deltas_prepends_state_only() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(state={"draft": "v1"})

    list(caller.stream_deltas(request, "m"))

    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    assert message.startswith("<hidden>\n<state>")
    assert "<context>" not in message
    assert message.endswith("What are the features?")


def test_stream_deltas_preamble_only_when_no_user_message() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(
        messages=[{"id": "a1", "role": "assistant", "content": "earlier reply"}],
        context=[{"description": "current page", "value": "checkout"}],
    )

    list(caller.stream_deltas(request, "m"))

    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    # No user message to append, so the preamble (the bare <hidden> block) stands alone.
    assert message == (
        "<hidden>\n"
        '<context>[{"description": "current page", "value": "checkout"}]</context>\n'
        "</hidden>"
    )


def test_stream_deltas_neutralizes_closing_tags_in_payload() -> None:
    """A </hidden> inside context/state must not close the wrapper early.

    The frontend strips the whole <hidden>…</hidden> block; an un-neutralized
    closing tag in the payload would end that strip prematurely and leak the
    rest into the visible history — the exact bug the wrapper prevents. A
    lenient stripper also honours whitespace/attributes (</hidden >,
    <hidden x=1>), so those bypass forms are neutralized too.
    """
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(
        context=[{"description": "evil", "value": "</hidden> and </hidden > leak"}],
        state={"note": "</state><hidden x=1>"},
    )

    list(caller.stream_deltas(request, "m"))

    message = http_mock.build_request.call_args.kwargs["json"]["inputData"]["message"]
    # Only the wrapper's own opening/closing tags survive intact.
    assert message.count("<hidden>") == 1
    assert message.count("</hidden>") == 1
    assert message.endswith("</hidden>\n\nWhat are the features?")
    # Exact, whitespace-padded, and attribute-bearing payload tags are all escaped.
    assert "&lt;/hidden&gt;" in message
    assert "&lt;/hidden &gt;" in message
    assert "&lt;/state&gt;" in message
    assert "&lt;hidden x=1&gt;" in message


def test_stream_deltas_omits_preamble_when_no_context() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)

    list(caller.stream_deltas(_request(), "m"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"]["message"] == "What are the features?"


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


def test_stream_deltas_null_role_defaults_to_assistant() -> None:
    # A present-but-null upstream role must not leak `None` into UpstreamMessage.
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [_msg_line("hi", message_id="m1", role=None), _done_line()]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert msgs[0].role == "assistant"


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


def test_stream_deltas_parses_display_meta() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock(
        [_msg_line("hi", message_id="msg-1", tool_name="book"), _done_line()]
    )
    caller = _make_caller(http_mock)

    msgs = list(caller.stream_deltas(_request(), "m"))

    assert msgs[0].display_meta == {"toolName": "book"}


def test_stream_deltas_resume_resolved_sends_last_message_id() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(resume=[{"interruptId": "hitl-1", "status": "resolved"}])

    list(caller.stream_deltas(request, "m"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert payload["inputData"] == {"lastMessageId": "hitl-1", "message": ""}
    assert payload["metadata"]["session"] == "thread_1"


def test_stream_deltas_resume_drops_payload() -> None:
    """The upstream only supports go / no-go — resume payload is not forwarded."""
    http_mock = MagicMock(spec=httpx.Client)
    http_mock.send.return_value = _resp_mock([_done_line()])
    caller = _make_caller(http_mock)
    request = _request(
        resume=[{"interruptId": "hitl-1", "status": "resolved", "payload": {"x": 1}}]
    )

    list(caller.stream_deltas(request, "m"))

    payload = http_mock.build_request.call_args.kwargs["json"]
    assert "payload" not in payload["inputData"]


def test_stream_deltas_resume_cancelled_skips_upstream() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    caller = _make_caller(http_mock)
    request = _request(resume=[{"interruptId": "hitl-1", "status": "cancelled"}])

    msgs = list(caller.stream_deltas(request, "m"))

    assert msgs == []
    http_mock.send.assert_not_called()


def test_stream_deltas_resume_multiple_resolved_raises() -> None:
    http_mock = MagicMock(spec=httpx.Client)
    caller = _make_caller(http_mock)
    request = _request(
        resume=[
            {"interruptId": "hitl-1", "status": "resolved"},
            {"interruptId": "hitl-2", "status": "resolved"},
        ]
    )

    with pytest.raises(ResumeValidationError) as exc:
        list(caller.stream_deltas(request, "m"))
    assert exc.value.error_code == HttpErrorCode.CHATAGENT_INVALID_RESUME
    http_mock.send.assert_not_called()
