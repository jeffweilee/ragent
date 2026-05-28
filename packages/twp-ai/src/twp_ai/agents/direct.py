"""DirectLLMAgent — agent for OpenAI-compatible LLM APIs.

Use this when you have a raw LLM API (like ragent's LLMClient) and want
twp-ai to manage the tool-call loop for you.

If you use a framework like LangGraph or CrewAI, write your own Agent
instead — those frameworks manage the loop internally, so you only need
to convert their output events to twp-ai events.

Flow for the form-fill scenario:
    RUN_STARTED
    Turn 1: LLM talks and/or calls tools
    CUSTOM("fill_form", {schema, data})   ← FE fills form
    Turn 2: LLM confirms                  ← only if tools were called
    RUN_FINISHED

To support a different scenario with different flow, write a new Agent
class rather than subclassing or modifying this one.
"""

from __future__ import annotations

import json
from collections.abc import Generator

from .._compose import Turn, build_messages, build_tool_defs, inject_tool_results, new_id
from ..callers.protocol import LLMCaller
from ..events import CustomEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent, to_sse
from ..schemas import ChatRequest


class DirectLLMAgent:
    """Calls the LLM directly and manages the tool-call loop.

    Suitable for: ragent LLMClient, any OpenAI-compatible API.
    Not needed for: LangGraph, CrewAI, Pydantic AI (they manage loops internally).
    """

    def __init__(self, caller: LLMCaller) -> None:
        self._caller = caller

    def run(self, request: ChatRequest, model: str) -> Generator[str, None, None]:
        run_id = new_id()
        yield to_sse(RunStartedEvent(run_id=run_id))

        try:
            tool_defs = build_tool_defs(request.context)
            messages = build_messages(request, _system_prompt(request))

            # Turn 1 — LLM talks and/or calls tools
            turn1 = Turn(self._caller.stream_events(messages, tool_defs, model))
            yield from turn1

            # CUSTOM event per tool call → FE fills form
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

            # Turn 2 — LLM confirms (only if tools were called)
            if turn1.tool_calls:
                inject_tool_results(messages, turn1.tool_calls)
                turn2 = Turn(self._caller.stream_events(messages, [], model))
                yield from turn2

            yield to_sse(RunFinishedEvent(run_id=run_id))

        except Exception as exc:
            yield to_sse(RunErrorEvent(message=str(exc), code=type(exc).__name__))


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
