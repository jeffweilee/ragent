"""twp-ai run input schemas for twp-ai."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class TwpAiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


class Tool(TwpAiModel):
    """twp-ai client-provided tool definition."""

    name: str
    description: str
    parameters: Any


class ToolResult(TwpAiModel):
    """Result returned by a frontend tool runtime."""

    tool_call_id: str
    tool_name: str | None = None
    status: str = "success"
    content: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class FunctionCall(TwpAiModel):
    name: str
    arguments: str


class ToolCall(TwpAiModel):
    id: str
    type: Literal["function"]
    function: FunctionCall
    encrypted_value: str | None = None


class Message(TwpAiModel):
    # Client-supplied and OPTIMISTIC: the frontend mints this id (e.g.
    # `optimistic-<uuid>`) so it can render a message before the upstream has
    # persisted it. It is NOT authoritative — the proxy ignores it (only the
    # message text is forwarded) and the upstream assigns the canonical
    # `messageId` returned in the stream / session history. Never persist, dedup,
    # or correlate on this value; use the upstream id for any server-side keying.
    id: str | None = None
    role: Literal["developer", "system", "assistant", "user", "tool", "activity", "reasoning"]
    content: Any = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    error: str | None = None
    encrypted_value: str | None = None
    activity_type: str | None = None


class ContextItem(TwpAiModel):
    description: str
    value: str


class ResumeItem(TwpAiModel):
    """One human-in-the-loop interrupt the client is answering.

    `interrupt_id` echoes the `Interrupt.id` (upstream messageId) the run paused
    on. `resolved` continues the upstream run (sent as `lastMessageId`);
    `cancelled` drops it with no upstream call. `payload` is accepted but not
    forwarded — the upstream only supports go / no-go for now.
    """

    interrupt_id: str
    status: Literal["resolved", "cancelled"]
    payload: Any = None


class RunAgentInput(TwpAiModel):
    # Session id. Optional on the wire: a brand-new conversation has none yet, so
    # the client may omit it and the *server* assigns one (ragent mints it for
    # /chatagent/v3; see chatagent_v3 router). The assigned id comes back in the
    # RUN_STARTED event, and the client sends it on every subsequent turn. This
    # keeps a single owner for the session id (the server), never the upstream.
    thread_id: str | None = None
    run_id: str
    parent_run_id: str | None = None
    messages: list[Message]
    tools: list[Tool]
    state: Any
    context: list[ContextItem]
    forwarded_props: Any
    model: str | None = None
    # Human-in-the-loop continuation. When present, this turn answers a prior
    # interrupt instead of sending a new user message (see ResumeItem).
    resume: list[ResumeItem] | None = None
