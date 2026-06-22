"""ADKCaller Protocol — the contract for a pure upstream-relay backend.

Unlike LLMCaller (which manages a tool-call loop and yields typed
("text" | "tool_call", data) tuples), an ADKCaller fronts an upstream agent
service that owns its own agent loop. It yields UpstreamMessage objects parsed
from the upstream's SSE stream; ADKAgent maps these to AG-UI events.

Adding a new backend:
  1. Create a class with stream_deltas() matching the signature below.
  2. Pass it to ADKAgent(caller).
  No changes to the adapter, events, or schemas are needed.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..schemas import RunAgentInput


@dataclass
class UpstreamMessage:
    """Parsed upstream ChatAgent SSE message.

    One UpstreamMessage per entry in returnData.messages[] of each SSE event.
    """

    message_id: str
    role: Literal["assistant", "tool"]
    content: str | None = None
    agent_type: str | None = None  # messageMeta.langgraph_node: planner | commander | summarizer
    tool_name: str | None = None  # displayMeta.toolName
    display_meta: dict | None = None  # raw displayMeta — surfaced as Interrupt.metadata
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    is_interrupt: bool = False
    interrupt_message: str | None = None
    interrupt_content: str | None = None


class ADKCaller(Protocol):
    """Structural protocol satisfied by any upstream text-streaming backend.

    stream_deltas() yields UpstreamMessage objects parsed from the upstream's
    SSE stream. It raises on transport / upstream failure; ADKAgent converts
    the exception into a RUN_ERROR event.
    """

    def stream_deltas(
        self,
        request: RunAgentInput,
        model: str,
    ) -> Generator[UpstreamMessage, None, None]: ...
