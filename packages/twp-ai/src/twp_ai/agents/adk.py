"""ADKAgent — agent for an upstream agent service that streams rich delta events.

Use this when the agent logic lives in an external service (an "ADK"-style
upstream) and ragent only proxies it. The upstream owns its own tool loop;
this agent maps the upstream's SSE stream to the twp-ai AG-UI event lifecycle:

    RUN_STARTED
    [per message from upstream:]
      TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT* / TEXT_MESSAGE_END  (role=assistant with text)
      TOOL_CALL_START / TOOL_CALL_ARGS / TOOL_CALL_END               (upstream tool calls)
      TOOL_CALL_RESULT                                                (upstream tool results)
    RUN_FINISHED

Upstream agent types (planner / commander / summarizer) each produce a separate
TEXT_MESSAGE block identified by their upstream messageId. Human-in-the-loop
interrupts are surfaced as a standalone TEXT_MESSAGE containing the interrupt
prompt. Any caller exception surfaces as a single RUN_ERROR event.
"""

from __future__ import annotations

from collections.abc import Generator

from ..callers.adk import ADKCaller, UpstreamMessage
from ..events import (
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
    to_sse,
)
from ..schemas import RunAgentInput


class ADKAgent:
    """Relays an upstream agent's SSE stream as twp-ai AG-UI events."""

    def __init__(self, caller: ADKCaller) -> None:
        self._caller = caller

    def run(self, request: RunAgentInput, model: str) -> Generator[str, None, None]:
        yield to_sse(
            RunStartedEvent(
                run_id=request.run_id,
                thread_id=request.thread_id,
                parent_run_id=request.parent_run_id,
            )
        )
        try:
            yield from _relay(self._caller.stream_deltas(request, model))
            yield to_sse(RunFinishedEvent(run_id=request.run_id, thread_id=request.thread_id))
        except Exception as exc:
            yield to_sse(
                RunErrorEvent(
                    message=str(exc),
                    code=getattr(exc, "error_code", None) or type(exc).__name__,
                    run_id=request.run_id,
                    thread_id=request.thread_id,
                )
            )


def _relay(upstream: Generator[UpstreamMessage, None, None]) -> Generator[str, None, None]:
    """Map upstream messages to AG-UI events with message-boundary tracking."""
    open_msg_id: str | None = None
    # Maps function_name → tool_call_id so TOOL_CALL_RESULT can reference the right call.
    pending_calls: dict[str, str] = {}

    for msg in upstream:
        if open_msg_id is not None and msg.message_id != open_msg_id:
            yield to_sse(TextMessageEndEvent(message_id=open_msg_id))
            open_msg_id = None

        if msg.is_interrupt:
            if msg.interrupt_message:
                yield to_sse(TextMessageStartEvent(message_id=msg.message_id))
                yield to_sse(
                    TextMessageContentEvent(message_id=msg.message_id, delta=msg.interrupt_message)
                )
                yield to_sse(TextMessageEndEvent(message_id=msg.message_id))
            continue

        if msg.role == "assistant":
            if msg.content:
                if open_msg_id is None:
                    open_msg_id = msg.message_id
                    yield to_sse(TextMessageStartEvent(message_id=open_msg_id))
                yield to_sse(TextMessageContentEvent(message_id=msg.message_id, delta=msg.content))

            if msg.tool_calls and msg.finish_reason == "tool_calls":
                if open_msg_id is not None:
                    yield to_sse(TextMessageEndEvent(message_id=open_msg_id))
                    open_msg_id = None
                for i, tc in enumerate(msg.tool_calls):
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "unknown")
                    tc_id = f"{msg.message_id}-{i}"
                    pending_calls[fn_name] = tc_id
                    yield to_sse(
                        ToolCallStartEvent(
                            tool_call_id=tc_id,
                            tool_call_name=fn_name,
                            parent_message_id=msg.message_id,
                        )
                    )
                    if fn.get("arguments"):
                        yield to_sse(ToolCallArgsEvent(tool_call_id=tc_id, delta=fn["arguments"]))
                    yield to_sse(ToolCallEndEvent(tool_call_id=tc_id))

        elif msg.role == "tool" and msg.content is not None:
            tc_id = pending_calls.get(msg.tool_name or "", msg.message_id)
            yield to_sse(
                ToolCallResultEvent(
                    message_id=msg.message_id,
                    tool_call_id=tc_id,
                    content=msg.content,
                )
            )

    if open_msg_id is not None:
        yield to_sse(TextMessageEndEvent(message_id=open_msg_id))
