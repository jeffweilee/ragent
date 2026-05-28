"""LLMCaller Protocol and ToolDef — the contract every backend must satisfy.

Any object that implements stream_events() can be used as an LLMCaller.
The adapter never knows which provider is underneath.

Adding a new backend:
  1. Create a class with stream_events() matching the signature below.
  2. Pass it to create_router() or create_app().
  No changes to the adapter, events, or schemas are needed.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolDef:
    """Provider-agnostic tool definition built from context.tool_inputs.

    Each caller is responsible for converting this into its provider's
    native function/tool format (OpenAI, Anthropic, etc.).
    """

    name: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict)


class LLMCaller(Protocol):
    """Structural protocol satisfied by any LLM/agent backend.

    stream_events() yields exactly two internal event kinds:

        ("text",      delta: str)
            — a text chunk to forward as TEXT_MESSAGE_CONTENT

        ("tool_call", {"id": str, "name": str, "arguments": str})
            — a fully-accumulated tool call (emitted once at finish_reason)

    The caller owns:
      - Converting ToolDef → provider tool format
      - Accumulating streamed tool-call argument chunks
      - Auth, retries, and transport
      - Simulating tool calls via prompting if the backend has no native support
    """

    def stream_events(
        self,
        messages: list[dict],
        tools: list[ToolDef],
        model: str,
    ) -> Generator[tuple[str, Any], None, None]: ...
