import json
from collections.abc import Generator
from typing import Any

from fastapi.testclient import TestClient
from twp_ai.agents.direct import DirectLLMAgent
from twp_ai.app import create_app
from twp_ai.schemas import RunAgentInput


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], list[Any], str]] = []

    def stream_events(
        self,
        messages: list[dict],
        tools: list[Any],
        model: str,
    ) -> Generator[tuple[str, Any], None, None]:
        self.calls.append((messages.copy(), tools, model))
        last_message = messages[-1]
        if last_message.get("role") == "tool":
            yield ("text", "Filled the description.")
            return
        if len(self.calls) == 1:
            yield (
                "tool_call",
                {
                    "id": "call_1",
                    "name": "fill_form",
                    "arguments": '{"description":"Better copy"}',
                },
            )
            return


def _events(lines: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("data: ").strip()) for line in lines]


def _run_input() -> dict:
    return {
        "threadId": "thread_1",
        "runId": "run_1",
        "state": {"page": {"title": "Edit product"}},
        "messages": [{"id": "msg_user_1", "role": "user", "content": "Fill the description"}],
        "tools": [
            {
                "name": "fill_form",
                "description": "Use only when the user asks to edit the form.",
                "parameters": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                },
            }
        ],
        "context": [
            {
                "description": "Current page",
                "value": '{"title":"Edit product","fields":["description"]}',
            }
        ],
        "forwardedProps": {"source": "test"},
    }


def test_run_agent_input_accepts_twp_ai_tool_shape() -> None:
    request = RunAgentInput.model_validate(_run_input())

    assert request.thread_id == "thread_1"
    assert request.run_id == "run_1"
    assert request.messages[0].id == "msg_user_1"
    assert request.tools[0].name == "fill_form"
    assert request.tools[0].parameters["type"] == "object"
    assert request.context[0].description == "Current page"
    assert request.forwarded_props == {"source": "test"}


def test_run_agent_input_accepts_message_without_id() -> None:
    body = _run_input()
    body["messages"] = [{"role": "user", "content": "Fill the description"}]

    request = RunAgentInput.model_validate(body)

    assert request.messages[0].id is None
    assert request.messages[0].content == "Fill the description"


def test_run_agent_input_accepts_missing_thread_id() -> None:
    body = _run_input()
    del body["threadId"]

    request = RunAgentInput.model_validate(body)

    assert request.thread_id is None


def test_run_route_assigns_thread_id_when_omitted() -> None:
    # Server owns the thread id: an omitted threadId is assigned so RUN_STARTED
    # never carries a null id.
    client = TestClient(create_app(DirectLLMAgent(FakeCaller()), default_model="m"))
    body = _run_input()
    del body["threadId"]

    response = client.post("/run", json=body)

    first = json.loads(response.text.split("\n\n")[0].removeprefix("data: "))
    assert first["type"] == "RUN_STARTED"
    assert first["threadId"]


def test_direct_agent_emits_twp_ai_tool_lifecycle_events() -> None:
    caller = FakeCaller()
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(DirectLLMAgent(caller).run(request, "model-a")))
    event_types = [event["type"] for event in events]

    assert event_types == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "RUN_FINISHED",
    ]
    assert events[0]["threadId"] == "thread_1"
    assert events[0]["runId"] == "run_1"
    assert events[1]["toolCallId"] == "call_1"
    assert events[1]["toolCallName"] == "fill_form"
    assert events[2]["delta"] == '{"description":"Better copy"}'
    assert len(caller.calls) == 1


def test_direct_agent_preserves_client_tool_result_history() -> None:
    caller = FakeCaller()
    body = _run_input()
    body["messages"] = [
        *body["messages"],
        {
            "id": "assistant_tool_1",
            "role": "assistant",
            "content": None,
            "toolCalls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "fill_form",
                        "arguments": '{"description":"Better copy"}',
                    },
                }
            ],
        },
        {
            "id": "tool_result_1",
            "role": "tool",
            "toolCallId": "call_1",
            "content": '{"ok":true}',
        },
    ]
    request = RunAgentInput.model_validate(body)

    events = _events(list(DirectLLMAgent(caller).run(request, "model-a")))

    provider_messages = caller.calls[0][0]
    assert provider_messages[-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "fill_form",
                    "arguments": '{"description":"Better copy"}',
                },
            }
        ],
    }
    assert provider_messages[-1] == {
        "role": "tool",
        "content": '{"ok":true}',
        "tool_call_id": "call_1",
    }
    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]


def test_run_route_streams_agent_events() -> None:
    class RouteAgent:
        def __init__(self) -> None:
            self.seen_model = ""

        def run(self, request: RunAgentInput, model: str) -> Generator[str, None, None]:
            self.seen_model = model
            yield 'data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}\n\n'

    agent = RouteAgent()
    client = TestClient(create_app(agent, default_model="model-default"))

    response = client.post("/run", json=_run_input())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"RUN_STARTED"' in response.text
    assert agent.seen_model == "model-default"


class _BoomCaller:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def stream_events(self, messages, tools, model):
        raise self._error
        yield  # pragma: no cover — never reached, makes this a generator


def test_direct_agent_unclassified_error_yields_generic_run_error() -> None:
    caller = _BoomCaller(RuntimeError("boom: secret_token=abc123"))
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(DirectLLMAgent(caller).run(request, "model-a")))

    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "INTERNAL_ERROR"
    assert events[-1]["message"] == "internal error"
    assert "secret_token" not in events[-1]["message"]


def test_direct_agent_classified_error_exposes_message_and_code() -> None:
    class Boom(Exception):
        error_code = "LLM_TIMEOUT"

    caller = _BoomCaller(Boom("llm call timed out"))
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(DirectLLMAgent(caller).run(request, "model-a")))

    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "LLM_TIMEOUT"
    assert events[-1]["message"] == "llm call timed out"


def test_direct_agent_unclassified_error_is_logged_server_side(
    captured_logger_text, describe_logger_state
) -> None:
    caller = _BoomCaller(RuntimeError("boom: secret_token=abc123"))
    request = RunAgentInput.model_validate(_run_input())

    with captured_logger_text("twp_ai.agents.direct") as stream:
        list(DirectLLMAgent(caller).run(request, "model-a"))

    assert "secret_token" in stream.getvalue(), describe_logger_state("twp_ai.agents.direct")


def test_user_message_event_serialises_to_sse() -> None:
    from twp_ai.events import UserMessageEvent, to_sse

    frame = to_sse(UserMessageEvent(message_id="run_1-user", content="what are the features?"))
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload == {
        "type": "USER_MESSAGE",
        "messageId": "run_1-user",
        "content": "what are the features?",
        "role": "user",
    }
