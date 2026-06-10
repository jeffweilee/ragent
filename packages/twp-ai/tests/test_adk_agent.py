"""T-CAv3.2 — ADKAgent: AG-UI event mapping from upstream messages."""

import json
from collections.abc import Generator

from twp_ai.agents.adk import ADKAgent
from twp_ai.callers.adk import UpstreamMessage
from twp_ai.schemas import RunAgentInput


def _events(lines: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("data: ").strip()) for line in lines]


def _run_input() -> dict:
    return {
        "threadId": "thread_1",
        "runId": "run_1",
        "messages": [{"id": "m1", "role": "user", "content": "hello"}],
        "tools": [],
        "state": None,
        "context": [],
        "forwardedProps": None,
    }


class FakeADKCaller:
    def __init__(
        self,
        messages: list[UpstreamMessage] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._messages = messages or []
        self._error = error
        self.calls: list[tuple[RunAgentInput, str]] = []

    def stream_deltas(
        self, request: RunAgentInput, model: str
    ) -> Generator[UpstreamMessage, None, None]:
        self.calls.append((request, model))
        yield from self._messages
        if self._error is not None:
            raise self._error


def test_adk_agent_emits_text_lifecycle() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(message_id="msg-1", role="assistant", content="Hello "),
            UpstreamMessage(message_id="msg-1", role="assistant", content="world"),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert events[0]["threadId"] == "thread_1"
    assert events[0]["runId"] == "run_1"
    deltas = [event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]
    assert deltas == ["Hello ", "world"]
    message_ids = {event["messageId"] for event in events if "messageId" in event}
    assert len(message_ids) == 1  # same id across start/content/end


def test_adk_agent_uses_upstream_message_id() -> None:
    caller = FakeADKCaller(
        messages=[UpstreamMessage(message_id="upstream-42", role="assistant", content="hi")]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    msg_events = [e for e in events if "messageId" in e]
    assert all(e["messageId"] == "upstream-42" for e in msg_events)


def test_adk_agent_multi_agent_produces_separate_message_blocks() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="cmd-1",
                role="assistant",
                content="Executing step 1.",
                agent_type="commander",
            ),
            UpstreamMessage(
                message_id="sum-1",
                role="assistant",
                content="Summary.",
                agent_type="summarizer",
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    types = [e["type"] for e in events]
    assert types == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    starts = [e for e in events if e["type"] == "TEXT_MESSAGE_START"]
    assert starts[0]["messageId"] == "cmd-1"
    assert starts[1]["messageId"] == "sum-1"


def test_adk_agent_planner_emits_reasoning_lifecycle() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="plan-1",
                role="assistant",
                content="Planning...",
                agent_type="planner",
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_END",
        "REASONING_END",
        "RUN_FINISHED",
    ]
    content = next(e for e in events if e["type"] == "REASONING_MESSAGE_CONTENT")
    assert content["delta"] == "Planning..."
    assert content["messageId"] == "plan-1"
    msg_ids = {e["messageId"] for e in events if "messageId" in e}
    assert msg_ids == {"plan-1"}
    start = next(e for e in events if e["type"] == "REASONING_MESSAGE_START")
    assert start["role"] == "reasoning"


def test_adk_agent_planner_streams_deltas_in_one_reasoning_block() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="plan-1", role="assistant", content="Think ", agent_type="planner"
            ),
            UpstreamMessage(
                message_id="plan-1", role="assistant", content="harder", agent_type="planner"
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_END",
        "REASONING_END",
        "RUN_FINISHED",
    ]
    deltas = [e["delta"] for e in events if e["type"] == "REASONING_MESSAGE_CONTENT"]
    assert deltas == ["Think ", "harder"]


def test_adk_agent_planner_then_summarizer_closes_reasoning_before_text() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="plan-1", role="assistant", content="Planning...", agent_type="planner"
            ),
            UpstreamMessage(
                message_id="sum-1", role="assistant", content="Summary.", agent_type="summarizer"
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_END",
        "REASONING_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    reasoning_msg = next(e for e in events if e["type"] == "REASONING_MESSAGE_START")
    assert reasoning_msg["messageId"] == "plan-1"
    text_msg = next(e for e in events if e["type"] == "TEXT_MESSAGE_START")
    assert text_msg["messageId"] == "sum-1"


def test_adk_agent_tool_calls_produce_tool_call_events() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="msg-tc",
                role="assistant",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call-abc",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q":"test"}'},
                    }
                ],
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    types = [e["type"] for e in events]
    assert "TOOL_CALL_START" in types
    assert "TOOL_CALL_ARGS" in types
    assert "TOOL_CALL_END" in types
    tc_start = next(e for e in events if e["type"] == "TOOL_CALL_START")
    assert tc_start["toolCallName"] == "search"
    assert tc_start["parentMessageId"] == "msg-tc"
    assert tc_start["toolCallId"] == "call-abc"


def test_adk_agent_tool_result_produces_tool_call_result() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="msg-tc",
                role="assistant",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call-xyz",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            ),
            UpstreamMessage(
                message_id="msg-tr",
                role="tool",
                content="Search results here",
                tool_name="search",
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    result = next((e for e in events if e["type"] == "TOOL_CALL_RESULT"), None)
    assert result is not None
    assert result["content"] == "Search results here"
    # tool_call_id should match the TOOL_CALL_START id
    tc_start = next(e for e in events if e["type"] == "TOOL_CALL_START")
    assert result["toolCallId"] == tc_start["toolCallId"]


def test_adk_agent_hitl_interrupt_surfaces_as_text_message() -> None:
    caller = FakeADKCaller(
        messages=[
            UpstreamMessage(
                message_id="hitl-1",
                role="assistant",
                is_interrupt=True,
                interrupt_message="Please confirm before proceeding.",
            ),
        ]
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    types = [e["type"] for e in events]
    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "TEXT_MESSAGE_END" in types
    content = next(e for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert content["delta"] == "Please confirm before proceeding."


def test_adk_agent_passes_request_and_model_to_caller() -> None:
    caller = FakeADKCaller(
        messages=[UpstreamMessage(message_id="m", role="assistant", content="x")]
    )
    request = RunAgentInput.model_validate(_run_input())

    list(ADKAgent(caller).run(request, "model-z"))

    assert caller.calls[0][1] == "model-z"
    assert caller.calls[0][0].thread_id == "thread_1"


def test_adk_agent_caller_error_becomes_run_error() -> None:
    class Boom(Exception):
        error_code = "CHATAGENT_TIMEOUT"

    caller = FakeADKCaller(messages=[], error=Boom("upstream timed out"))
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "CHATAGENT_TIMEOUT"
    assert events[-1]["message"] == "upstream timed out"
    assert events[-1]["runId"] == "run_1"
    assert events[-1]["threadId"] == "thread_1"


def test_adk_agent_error_without_error_code_uses_exception_name() -> None:
    caller = FakeADKCaller(
        messages=[UpstreamMessage(message_id="m", role="assistant", content="partial")],
        error=RuntimeError("boom"),
    )
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "RuntimeError"
