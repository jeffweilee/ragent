"""SSE event types for the twp-ai protocol.

Adding a new event:
  1. Subclass BaseEvent with a unique Literal `type`.
  2. Add it to the Event union below.
  3. Emit it with to_sse() — nothing else changes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class BaseEvent(BaseModel):
    """Foundation for every twp-ai SSE event.

    extra="allow" lets twp-ai-compatible extensions pass through without
    breaking deserialization.
    """

    model_config = ConfigDict(extra="allow", alias_generator=_to_camel, populate_by_name=True)


class RunStartedEvent(BaseEvent):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"
    run_id: str
    thread_id: str
    parent_run_id: str | None = None
    input: Any | None = None


class TextMessageStartEvent(BaseEvent):
    type: Literal["TEXT_MESSAGE_START"] = "TEXT_MESSAGE_START"
    message_id: str
    role: Literal["assistant"] = "assistant"


class TextMessageContentEvent(BaseEvent):
    type: Literal["TEXT_MESSAGE_CONTENT"] = "TEXT_MESSAGE_CONTENT"
    message_id: str
    delta: str


class TextMessageEndEvent(BaseEvent):
    type: Literal["TEXT_MESSAGE_END"] = "TEXT_MESSAGE_END"
    message_id: str


class ToolCallStartEvent(BaseEvent):
    type: Literal["TOOL_CALL_START"] = "TOOL_CALL_START"
    tool_call_id: str
    tool_call_name: str
    parent_message_id: str | None = None


class ToolCallArgsEvent(BaseEvent):
    type: Literal["TOOL_CALL_ARGS"] = "TOOL_CALL_ARGS"
    tool_call_id: str
    delta: str


class ToolCallEndEvent(BaseEvent):
    type: Literal["TOOL_CALL_END"] = "TOOL_CALL_END"
    tool_call_id: str


class ToolCallResultEvent(BaseEvent):
    type: Literal["TOOL_CALL_RESULT"] = "TOOL_CALL_RESULT"
    message_id: str
    tool_call_id: str
    content: str
    role: Literal["tool"] = "tool"


class ReasoningStartEvent(BaseEvent):
    type: Literal["REASONING_START"] = "REASONING_START"


class ReasoningMessageStartEvent(BaseEvent):
    type: Literal["REASONING_MESSAGE_START"] = "REASONING_MESSAGE_START"
    message_id: str
    # AG-UI aligns the reasoning role on "reasoning" (the ReasoningMessage type in
    # both SDKs); the TS client's parser rejects "assistant".
    role: Literal["reasoning"] = "reasoning"


class ReasoningMessageContentEvent(BaseEvent):
    type: Literal["REASONING_MESSAGE_CONTENT"] = "REASONING_MESSAGE_CONTENT"
    message_id: str
    delta: str


class ReasoningMessageEndEvent(BaseEvent):
    type: Literal["REASONING_MESSAGE_END"] = "REASONING_MESSAGE_END"
    message_id: str


class ReasoningEndEvent(BaseEvent):
    type: Literal["REASONING_END"] = "REASONING_END"


class Interrupt(BaseEvent):
    """A human-in-the-loop pause the run is waiting on.

    Carried inside RUN_FINISHED.outcome when the upstream flags
    `humanInTheLoopMeta.isInterrupt`. `id` (the upstream messageId) is echoed
    back as the resume `interruptId`; `reason` is the upstream `finish_reason`
    (or "interrupt" when the turn carries no tool call).
    """

    id: str
    reason: str
    message: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] | None = None


class RunFinishedSuccess(BaseEvent):
    type: Literal["success"] = "success"


class RunFinishedInterrupt(BaseEvent):
    type: Literal["interrupt"] = "interrupt"
    interrupts: list[Interrupt]


RunFinishedOutcome = Annotated[
    RunFinishedSuccess | RunFinishedInterrupt, Field(discriminator="type")
]


class RunFinishedEvent(BaseEvent):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    run_id: str
    thread_id: str
    # Present on /chatagent/v3 (always success | interrupt); omitted on the
    # native agents that never set it (exclude_none drops the None default).
    outcome: RunFinishedOutcome | None = None


class RunErrorEvent(BaseEvent):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    message: str
    code: str | None = None
    run_id: str
    thread_id: str


# Discriminated union consumed by the FE for type-safe deserialization.
# Extend this when adding new event types.
Event = Annotated[
    RunStartedEvent
    | TextMessageStartEvent
    | TextMessageContentEvent
    | TextMessageEndEvent
    | ToolCallStartEvent
    | ToolCallArgsEvent
    | ToolCallEndEvent
    | ToolCallResultEvent
    | ReasoningStartEvent
    | ReasoningMessageStartEvent
    | ReasoningMessageContentEvent
    | ReasoningMessageEndEvent
    | ReasoningEndEvent
    | RunFinishedEvent
    | RunErrorEvent,
    Field(discriminator="type"),
]


def to_sse(event: BaseEvent) -> str:
    """Serialise any event to an SSE data line."""
    return f"data: {event.model_dump_json(by_alias=True, exclude_none=True)}\n\n"
