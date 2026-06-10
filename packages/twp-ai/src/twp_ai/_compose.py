"""Composable streaming primitives for building twp-ai run handlers.

Core primitive: Turn
    Wraps one LLM stream. You yield from it (gets SSE to the FE),
    and after it finishes, turn.tool_calls is populated.

    turn = Turn(caller.stream_events(messages, tools, model))
    yield from turn          # FE receives TEXT_MESSAGE_* events
    for tc in turn.tool_calls:   # now you know what was called
        ...

Helpers: build_system_prompt, build_messages, build_tool_defs
    Shared plumbing used by every handler.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

from .callers.protocol import ToolDef
from .events import TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent, to_sse
from .schemas import Message, RunAgentInput, ToolCall


def new_id() -> str:
    return str(uuid.uuid4())


class Turn:
    """One LLM stream turn: yields SSE strings while collecting side-effects.

    Iterate with `yield from turn`.
    After exhaustion, read `turn.text` and `turn.tool_calls`.

    Why this design: Python generators can't both yield AND return values.
    Turn solves this by being an iterable object that accumulates results
    into attributes as it streams, so callers read them after `yield from`.
    """

    def __init__(self, llm_iter: Generator[tuple[str, Any], None, None]) -> None:
        self._iter = llm_iter
        self.text: str = ""
        self.tool_calls: list[dict] = []

    def __iter__(self) -> Generator[str, None, None]:
        msg_id = new_id()
        has_text = False

        for event_type, data in self._iter:
            if event_type == "text":
                if not has_text:
                    yield to_sse(TextMessageStartEvent(message_id=msg_id))
                    has_text = True
                self.text += data
                yield to_sse(TextMessageContentEvent(message_id=msg_id, delta=data))
            elif event_type == "tool_call":
                self.tool_calls.append(data)

        if has_text:
            yield to_sse(TextMessageEndEvent(message_id=msg_id))


def build_system_prompt(request: RunAgentInput) -> str:
    """Fold the run input's tools, context, and state into a system prompt.

    This is the native DirectLLMAgent prompt (form-filling persona + tool-call
    policy). Upstream-proxy handlers that must not impose a persona build their
    own preamble instead.
    """
    lines = ["You are a helpful assistant that helps users complete tasks and fill forms.", ""]

    if request.tools:
        lines.append("Available tools:")
        for tool in request.tools:
            lines.append(f"  - {tool.name}: {tool.description}")
        lines.append("")
        lines.append(
            "Only call a tool when the user explicitly asks to change, fill, update, "
            "submit, clear, or otherwise modify the current application state. "
            "If the user asks about the page or asks an unrelated question, answer in text."
        )
    else:
        lines.append("Answer the user helpfully.")

    if request.context:
        lines.append("")
        context_json = json.dumps(
            [item.model_dump(by_alias=True) for item in request.context],
            ensure_ascii=False,
        )
        lines.append(f"Context: {context_json}")

    if request.state is not None:
        lines.append("")
        lines.append(f"State: {json.dumps(request.state, ensure_ascii=False)}")

    return "\n".join(lines)


def build_messages(request: RunAgentInput, system_prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        *[_message_to_provider_dict(message) for message in request.messages],
    ]


def build_tool_defs(request: RunAgentInput) -> list[ToolDef]:
    return [
        ToolDef(name=tool.name, description=tool.description, schema=tool.parameters)
        for tool in request.tools
    ]


def _message_to_provider_dict(message: Message) -> dict:
    result = {"role": message.role, "content": message.content}

    if message.name is not None:
        result["name"] = message.name

    if message.role == "assistant" and message.tool_calls:
        result["tool_calls"] = [
            _tool_call_to_provider_dict(tool_call) for tool_call in message.tool_calls
        ]

    if message.role == "tool" and message.tool_call_id:
        result["tool_call_id"] = message.tool_call_id

    return result


def _tool_call_to_provider_dict(tool_call: ToolCall) -> dict:
    return {
        "id": tool_call.id,
        "type": tool_call.type,
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }
