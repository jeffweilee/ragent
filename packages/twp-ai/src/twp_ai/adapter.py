"""Provider-agnostic orchestration: ChatRequest → LLMCaller → SSE events.

This layer knows nothing about HTTP, OpenAI, or any specific LLM.
It only knows about:
  - The ChatRequest schema (what the FE sends)
  - The LLMCaller Protocol (how to get text/tool_call tuples)
  - The event types (what to emit as SSE)
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator

from .callers.protocol import LLMCaller, ToolDef
from .events import (
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    to_sse,
)
from .schemas import ChatContext, ChatRequest


def _new_id() -> str:
    return str(uuid.uuid4())


def _build_tool_defs(context: ChatContext) -> list[ToolDef]:
    """Build provider-agnostic ToolDef list from context.tool_inputs."""
    return [
        ToolDef(
            name=name,
            description=f"Fill the '{name}' form with data extracted from the user's request.",
            schema=context.tool_inputs[name].schema_ if name in context.tool_inputs else {},
        )
        for name in context.tools
    ]


def _build_system_prompt(context: ChatContext) -> str:
    lines = [
        "You are a helpful assistant that helps users complete tasks and fill forms.",
        "",
    ]
    if context.tools:
        lines.append("Available tools:")
        for name in context.tools:
            lines.append(f"  - {name}: extract relevant data and call this tool.")
        lines.append("")
        lines.append(
            "When the user asks to create or fill something, call the matching tool "
            "with the extracted data, then confirm what was filled in."
        )
    else:
        lines.append("Answer the user helpfully.")
    if context.app_meta:
        lines.append("")
        lines.append(f"App context: {json.dumps(context.app_meta, ensure_ascii=False)}")
    return "\n".join(lines)


def _tool_schema(context: ChatContext, tool_name: str) -> dict:
    ti = context.tool_inputs.get(tool_name)
    return ti.schema_ if ti else {}


def stream_chat_events(
    request: ChatRequest,
    model: str,
    llm_caller: LLMCaller,
) -> Generator[str, None, None]:
    """Yield SSE-formatted strings for the full chat interaction.

    Flow:
        RUN_STARTED
        [TEXT_MESSAGE_START → TEXT_MESSAGE_CONTENT × N → TEXT_MESSAGE_END]
        [CUSTOM("fill_form", {schema, data})]          ← FE fills form here
        [TEXT_MESSAGE_START → … → TEXT_MESSAGE_END]    ← LLM confirms
        RUN_FINISHED   |   RUN_ERROR
    """
    run_id = _new_id()
    yield to_sse(RunStartedEvent(run_id=run_id))

    try:
        tool_defs = _build_tool_defs(request.context)
        system_prompt = _build_system_prompt(request.context)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        # ── Turn 1 ─────────────────────────────────────────────────────────
        msg_id = _new_id()
        has_text = False
        text_parts: list[str] = []
        tool_calls_seen: list[dict] = []

        for event_type, data in llm_caller.stream_events(messages, tool_defs, model):
            if event_type == "text":
                if not has_text:
                    yield to_sse(TextMessageStartEvent(message_id=msg_id))
                    has_text = True
                text_parts.append(data)
                yield to_sse(TextMessageContentEvent(message_id=msg_id, delta=data))
            elif event_type == "tool_call":
                tool_calls_seen.append(data)

        if has_text:
            yield to_sse(TextMessageEndEvent(message_id=msg_id))

        # ── CUSTOM event per tool call ──────────────────────────────────────
        for tc in tool_calls_seen:
            tool_name = tc["name"]
            try:
                form_data = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                form_data = {}
            yield to_sse(
                CustomEvent(
                    name=tool_name,
                    value={"schema": _tool_schema(request.context, tool_name), "data": form_data},
                )
            )

        # ── Turn 2: continuation after tool calls ───────────────────────────
        if tool_calls_seen:
            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in tool_calls_seen
                    ],
                }
            )
            for tc in tool_calls_seen:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps({"status": "ok"}),
                    }
                )

            msg_id2 = _new_id()
            has_text2 = False
            # No tools in the continuation turn — force text-only response.
            for event_type, data in llm_caller.stream_events(messages, [], model):
                if event_type == "text":
                    if not has_text2:
                        yield to_sse(TextMessageStartEvent(message_id=msg_id2))
                        has_text2 = True
                    yield to_sse(TextMessageContentEvent(message_id=msg_id2, delta=data))
            if has_text2:
                yield to_sse(TextMessageEndEvent(message_id=msg_id2))

        yield to_sse(RunFinishedEvent(run_id=run_id))

    except Exception as exc:
        yield to_sse(RunErrorEvent(message=str(exc), code=type(exc).__name__))
