"""Composable streaming primitives for building chat handlers.

Core primitive: Turn
    Wraps one LLM stream. You yield from it (gets SSE to the FE),
    and after it finishes, turn.tool_calls is populated.

    turn = Turn(caller.stream_events(messages, tools, model))
    yield from turn          # FE receives TEXT_MESSAGE_* events
    for tc in turn.tool_calls:   # now you know what was called
        ...

Helpers: build_messages, build_tool_defs, inject_tool_results
    Shared plumbing used by every handler.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

from .callers.protocol import ToolDef
from .events import TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent, to_sse
from .schemas import ChatContext, ChatRequest


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


def build_messages(request: ChatRequest, system_prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        *[{"role": m.role, "content": m.content} for m in request.messages],
    ]


def build_tool_defs(context: ChatContext) -> list[ToolDef]:
    return [
        ToolDef(
            name=name,
            description=f"Fill the '{name}' form with data extracted from the user's request.",
            schema=context.tool_inputs[name].schema_ if name in context.tool_inputs else {},
        )
        for name in context.tools
    ]


def inject_tool_results(messages: list[dict], tool_calls: list[dict]) -> None:
    """Append assistant tool-call turn + synthetic results into messages in-place.

    Tells the LLM "these tools were called and succeeded" so it can continue.
    """
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls
            ],
        }
    )
    for tc in tool_calls:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps({"status": "ok"}),
            }
        )
