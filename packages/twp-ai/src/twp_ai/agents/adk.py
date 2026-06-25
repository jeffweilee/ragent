"""ADKAgent — agent for an upstream agent service that streams rich delta events.

Use this when the agent logic lives in an external service (an "ADK"-style
upstream) and ragent only proxies it. The upstream owns its own tool loop;
this agent maps the upstream's SSE stream to the twp-ai AG-UI event lifecycle:

    RUN_STARTED
    [per message from upstream:]
      REASONING_START / REASONING_MESSAGE_START / REASONING_MESSAGE_CONTENT* /
        REASONING_MESSAGE_END / REASONING_END                       (planner node text)
      TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT* / TEXT_MESSAGE_END  (other assistant text)
      TOOL_CALL_START / TOOL_CALL_ARGS / TOOL_CALL_END               (upstream tool calls)
      TOOL_CALL_RESULT                                                (upstream tool results)
    RUN_FINISHED (outcome: success | interrupt)

Upstream agent types (planner / commander / summarizer) each produce a separate
block identified by their upstream messageId. The `planner` node is the agent's
plan/reasoning step, so it is surfaced as a REASONING_* block; every other node
becomes a TEXT_MESSAGE block. A human-in-the-loop interrupt
(`humanInTheLoopMeta.isInterrupt`) does not get its own block — the run ends with
RUN_FINISHED carrying an `interrupt` outcome that lists each pending Interrupt
(while the interrupt message's own content / tool-call deltas still stream). A
run with no interrupt ends with a `success` outcome. Any caller exception
surfaces as a single RUN_ERROR event.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from ..callers.adk import ADKCaller, UpstreamMessage
from ..events import (
    Interrupt,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunFinishedEvent,
    RunFinishedInterrupt,
    RunFinishedSuccess,
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
from ..roles import node_to_role
from ..schemas import RunAgentInput
from ._run_error import run_error_event

logger = logging.getLogger(__name__)


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
        interrupts: list[Interrupt] = []
        try:
            yield from _relay(self._caller.stream_deltas(request, model), interrupts)
            outcome = (
                RunFinishedInterrupt(interrupts=interrupts) if interrupts else RunFinishedSuccess()
            )
            yield to_sse(
                RunFinishedEvent(
                    run_id=request.run_id, thread_id=request.thread_id, outcome=outcome
                )
            )
        except Exception as exc:
            yield to_sse(
                run_error_event(
                    exc, run_id=request.run_id, thread_id=request.thread_id, logger=logger
                )
            )


def _relay(
    upstream: Generator[UpstreamMessage, None, None], interrupts: list[Interrupt]
) -> Generator[str, None, None]:
    """Map upstream messages to AG-UI events with message-boundary tracking.

    Human-in-the-loop interrupts are appended to `interrupts` (surfaced in
    RUN_FINISHED.outcome by the caller); the interrupt message's own content /
    tool-call deltas still stream normally.
    """
    open_msg_id: str | None = None
    open_kind: str | None = None  # "text" | "reasoning" — how the open block was started
    # Maps function_name → FIFO list of tc_ids so same-named calls resolve in order.
    pending_calls: dict[str, list[str]] = {}

    def _close_block() -> Generator[str, None, None]:
        nonlocal open_msg_id, open_kind
        mid, kind = open_msg_id, open_kind
        open_msg_id = open_kind = None
        if mid is None:
            return
        if kind == "reasoning":
            yield to_sse(ReasoningMessageEndEvent(message_id=mid))
            yield to_sse(ReasoningEndEvent())
        else:
            yield to_sse(TextMessageEndEvent(message_id=mid))

    for msg in upstream:
        if open_msg_id is not None and msg.message_id != open_msg_id:
            yield from _close_block()

        if msg.is_interrupt:
            interrupts.append(_to_interrupt(msg))

        if msg.role == "assistant":
            if msg.content:
                if node_to_role(msg.role, msg.agent_type) == "reasoning":
                    if open_msg_id is None:
                        open_msg_id, open_kind = msg.message_id, "reasoning"
                        yield to_sse(ReasoningStartEvent())
                        yield to_sse(ReasoningMessageStartEvent(message_id=open_msg_id))
                    yield to_sse(
                        ReasoningMessageContentEvent(message_id=msg.message_id, delta=msg.content)
                    )
                else:
                    if open_msg_id is None:
                        open_msg_id, open_kind = msg.message_id, "text"
                        yield to_sse(TextMessageStartEvent(message_id=open_msg_id))
                    yield to_sse(
                        TextMessageContentEvent(message_id=msg.message_id, delta=msg.content)
                    )

            if msg.tool_calls and msg.finish_reason == "tool_calls":
                if open_msg_id is not None:
                    yield from _close_block()
                for i, tc in enumerate(msg.tool_calls):
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "unknown")
                    tc_id = _tool_call_id(msg.message_id, i, tc)
                    pending_calls.setdefault(fn_name, []).append(tc_id)
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
            fn_queue = pending_calls.get(msg.tool_name or "")
            tc_id = fn_queue.pop(0) if fn_queue else msg.message_id
            yield to_sse(
                ToolCallResultEvent(
                    message_id=msg.message_id,
                    tool_call_id=tc_id,
                    content=msg.content,
                )
            )

    if open_msg_id is not None:
        yield from _close_block()


def _tool_call_id(message_id: str, index: int, tool_call: dict) -> str:
    """Resolve a tool call's id, falling back to ``{message_id}-{index}``.

    Shared by the stream (TOOL_CALL_START) and the interrupt outcome so a tool
    call whose upstream ``id`` is absent gets the SAME synthetic id on both
    surfaces — otherwise the FE cannot correlate ``Interrupt.toolCallId`` with
    the streamed tool call.
    """
    return tool_call.get("id") or f"{message_id}-{index}"


def _to_interrupt(msg: UpstreamMessage) -> Interrupt:
    """Build a RUN_FINISHED interrupt from an upstream HITL message."""
    tool_call_id = _tool_call_id(msg.message_id, 0, msg.tool_calls[0]) if msg.tool_calls else None
    return Interrupt(
        id=msg.message_id,
        reason=msg.finish_reason or "interrupt",
        message=msg.interrupt_message,
        tool_call_id=tool_call_id,
        metadata=msg.display_meta or None,
    )
