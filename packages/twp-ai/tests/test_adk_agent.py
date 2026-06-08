"""T-CAv3.2 — ADKAgent: twp-ai text lifecycle from a delta-only caller."""

import json
from collections.abc import Generator

from twp_ai.agents.adk import ADKAgent
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
    def __init__(self, deltas: list[str] | None = None, error: Exception | None = None) -> None:
        self._deltas = deltas or []
        self._error = error
        self.calls: list[tuple[RunAgentInput, str]] = []

    def stream_deltas(self, request: RunAgentInput, model: str) -> Generator[str, None, None]:
        self.calls.append((request, model))
        yield from self._deltas
        if self._error is not None:
            raise self._error


def test_adk_agent_emits_text_lifecycle() -> None:
    caller = FakeADKCaller(deltas=["Hello ", "world"])
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
    assert len(message_ids) == 1  # same minted id across start/content/end


def test_adk_agent_passes_request_and_model_to_caller() -> None:
    caller = FakeADKCaller(deltas=["x"])
    request = RunAgentInput.model_validate(_run_input())

    list(ADKAgent(caller).run(request, "model-z"))

    assert caller.calls[0][1] == "model-z"
    assert caller.calls[0][0].thread_id == "thread_1"


def test_adk_agent_caller_error_becomes_run_error() -> None:
    class Boom(Exception):
        error_code = "CHATAGENT_TIMEOUT"

    caller = FakeADKCaller(deltas=[], error=Boom("upstream timed out"))
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "CHATAGENT_TIMEOUT"
    assert events[-1]["message"] == "upstream timed out"
    assert events[-1]["runId"] == "run_1"
    assert events[-1]["threadId"] == "thread_1"


def test_adk_agent_error_without_error_code_uses_exception_name() -> None:
    caller = FakeADKCaller(deltas=["partial"], error=RuntimeError("boom"))
    request = RunAgentInput.model_validate(_run_input())

    events = _events(list(ADKAgent(caller).run(request, "m")))

    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "RuntimeError"
