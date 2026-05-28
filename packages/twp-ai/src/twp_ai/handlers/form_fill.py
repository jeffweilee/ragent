"""Handler: chat + form fill.

Scenario: user describes what they want → LLM calls fill_form tool →
FE receives CUSTOM event and fills the form → LLM confirms.

Flow:
    RUN_STARTED
    Turn 1: LLM streams text (optional) and/or calls fill_form
    CUSTOM("fill_form", {schema, data})   ← FE fills the form
    Turn 2: LLM confirms what was filled  ← only if tool was called
    RUN_FINISHED

To write your own handler for a different scenario, copy this file,
change the flow, and pass it to create_router(caller, handler=your_handle).
"""

from __future__ import annotations

import json
from collections.abc import Generator

from ..callers.protocol import LLMCaller
from ..compose import Turn, build_messages, build_tool_defs, inject_tool_results, new_id
from ..events import CustomEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent, to_sse
from ..schemas import ChatRequest


def _system_prompt(request: ChatRequest) -> str:
    ctx = request.context
    lines = ["You are a helpful assistant that helps users complete tasks and fill forms.", ""]

    if ctx.tools:
        lines.append("Available tools:")
        for name in ctx.tools:
            lines.append(f"  - {name}: extract relevant data and call this tool.")
        lines.append("")
        lines.append(
            "When the user asks to fill something, call the matching tool "
            "with the extracted data, then confirm what was filled."
        )
    else:
        lines.append("Answer the user helpfully.")

    if ctx.app_meta:
        lines.append("")
        lines.append(f"App context: {json.dumps(ctx.app_meta, ensure_ascii=False)}")

    return "\n".join(lines)


def handle(
    request: ChatRequest,
    model: str,
    caller: LLMCaller,
) -> Generator[str, None, None]:
    run_id = new_id()
    yield to_sse(RunStartedEvent(run_id=run_id))

    try:
        messages = build_messages(request, _system_prompt(request))
        tool_defs = build_tool_defs(request.context)

        # Turn 1 — LLM talks and/or calls tools
        turn1 = Turn(caller.stream_events(messages, tool_defs, model))
        yield from turn1

        # Emit CUSTOM for every tool the LLM called
        for tc in turn1.tool_calls:
            schema = (
                request.context.tool_inputs[tc["name"]].schema_
                if tc["name"] in request.context.tool_inputs
                else {}
            )
            try:
                data = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                data = {}
            yield to_sse(CustomEvent(name=tc["name"], value={"schema": schema, "data": data}))

        # Turn 2 — LLM confirms (only runs if tools were called)
        if turn1.tool_calls:
            inject_tool_results(messages, turn1.tool_calls)
            turn2 = Turn(caller.stream_events(messages, [], model))
            yield from turn2

        yield to_sse(RunFinishedEvent(run_id=run_id))

    except Exception as exc:
        yield to_sse(RunErrorEvent(message=str(exc), code=type(exc).__name__))
