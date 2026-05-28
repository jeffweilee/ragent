"""Core streaming adapter: request → LLM (with tool calls) → AG-UI events."""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

import httpx

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


# ---------------------------------------------------------------------------
# Prompt / tool-definition builders
# ---------------------------------------------------------------------------


def _build_llm_tools(context: ChatContext) -> list[dict]:
    """Convert context.tool_inputs into OpenAI function definitions."""
    tools = []
    for name in context.tools:
        tool_input = context.tool_inputs.get(name)
        schema: dict[str, Any] = tool_input.schema_ if tool_input else {}
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Fill the '{name}' form with data extracted from the user's request.",
                    "parameters": schema,
                },
            }
        )
    return tools


def _build_system_prompt(context: ChatContext) -> str:
    lines = [
        "You are a helpful assistant that helps users complete tasks and fill forms.",
        "",
    ]

    if context.tools:
        lines.append("Available tools:")
        for tool_name in context.tools:
            lines.append(
                f"  - {tool_name}: extract the relevant data from the user's request and call this tool."
            )
        lines.append("")
        lines.append(
            "When the user asks to create or fill something, call the matching tool with the "
            "extracted data, then confirm what you filled in."
        )
    else:
        lines.append("Answer the user helpfully.")

    if context.app_meta:
        lines.append("")
        lines.append(f"Application context: {json.dumps(context.app_meta, ensure_ascii=False)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Low-level LLM streaming
# ---------------------------------------------------------------------------


def _parse_sse_line(line: str) -> dict | None:
    """Parse a single SSE data line into a JSON dict, or None to skip."""
    if not line.startswith("data:"):
        return None
    data_str = line[len("data:") :].strip()
    if data_str == "[DONE]":
        return {"_done": True}
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        return None


def _stream_llm_turn(
    messages: list[dict],
    tools: list[dict],
    model: str,
    llm_url: str,
    api_key: str,
    http: httpx.Client,
) -> Generator[tuple[str, Any], None, None]:
    """Stream one LLM turn and yield typed tuples:

    ("text",      delta_str)              — a text chunk
    ("tool_call", {"id","name","arguments"}) — a completed tool call (emitted at finish)
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    tool_calls_acc: dict[int, dict[str, str]] = {}

    with http.stream(
        "POST",
        llm_url,
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = _parse_sse_line(line)
            if chunk is None:
                continue
            if chunk.get("_done"):
                break

            choices = chunk.get("choices") or []
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}
            finish_reason = choice.get("finish_reason")

            content = delta.get("content")
            if content:
                yield ("text", content)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                if tc.get("id"):
                    tool_calls_acc[idx]["id"] = tc["id"]
                func = tc.get("function") or {}
                if func.get("name"):
                    tool_calls_acc[idx]["name"] += func["name"]
                if func.get("arguments"):
                    tool_calls_acc[idx]["arguments"] += func["arguments"]

            if finish_reason in ("stop", "tool_calls", "length"):
                for _, tc_data in sorted(tool_calls_acc.items()):
                    yield ("tool_call", tc_data)
                break


# ---------------------------------------------------------------------------
# Main public generator
# ---------------------------------------------------------------------------


def stream_chat_events(
    request: ChatRequest,
    model: str,
    llm_url: str,
    api_key: str,
) -> Generator[str, None, None]:
    """Yield SSE-formatted strings for the complete chat interaction.

    Flow:
        RUN_STARTED
        [TEXT_MESSAGE_START → TEXT_MESSAGE_CONTENT × N → TEXT_MESSAGE_END]  (if LLM emits text)
        [CUSTOM("fill_form", {schema, data})]                                 (if LLM calls a tool)
        [TEXT_MESSAGE_START → … → TEXT_MESSAGE_END]                           (continuation after tool)
        RUN_FINISHED   (or RUN_ERROR on exception)
    """
    run_id = _new_id()
    yield to_sse(RunStartedEvent(run_id=run_id))

    try:
        llm_tools = _build_llm_tools(request.context)
        system_prompt = _build_system_prompt(request.context)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        with httpx.Client() as http:
            # ── Turn 1 ──────────────────────────────────────────────────────
            msg_id = _new_id()
            has_text = False
            text_parts: list[str] = []
            tool_calls_seen: list[dict] = []

            for event_type, data in _stream_llm_turn(
                messages, llm_tools, model, llm_url, api_key, http
            ):
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

            # ── Emit CUSTOM events for each tool call ───────────────────────
            for tc in tool_calls_seen:
                tool_name = tc["name"]
                schema: dict[str, Any] = {}
                if tool_name in request.context.tool_inputs:
                    schema = request.context.tool_inputs[tool_name].schema_
                try:
                    form_data = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    form_data = {}

                yield to_sse(CustomEvent(name=tool_name, value={"schema": schema, "data": form_data}))

            # ── Turn 2: continuation after tool calls ────────────────────────
            if tool_calls_seen:
                # Append assistant message that triggered the tool call(s)
                assistant_content: str | None = "".join(text_parts) or None
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_content,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            }
                            for tc in tool_calls_seen
                        ],
                    }
                )

                # Inject synthetic tool results so the LLM can continue
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

                # No tools passed → force text-only continuation
                for event_type, data in _stream_llm_turn(
                    messages, [], model, llm_url, api_key, http
                ):
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
