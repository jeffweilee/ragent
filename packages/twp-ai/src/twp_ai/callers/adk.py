"""ADKCaller Protocol — the contract for a pure text-streaming proxy backend.

Unlike LLMCaller (which manages a tool-call loop and yields typed
("text" | "tool_call", data) tuples), an ADKCaller fronts an upstream agent
service that owns its own agent loop. It only relays assistant text deltas
back to the caller; ADKAgent wraps each delta in the twp-ai text lifecycle.

Adding a new backend:
  1. Create a class with stream_deltas() matching the signature below.
  2. Pass it to ADKAgent(caller).
  No changes to the adapter, events, or schemas are needed.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Protocol

from ..schemas import RunAgentInput


class ADKCaller(Protocol):
    """Structural protocol satisfied by any upstream text-streaming backend.

    stream_deltas() yields assistant text chunks (forwarded as
    TEXT_MESSAGE_CONTENT). It raises on transport / upstream failure; ADKAgent
    converts the exception into a RUN_ERROR event.
    """

    def stream_deltas(
        self,
        request: RunAgentInput,
        model: str,
    ) -> Generator[str, None, None]: ...
