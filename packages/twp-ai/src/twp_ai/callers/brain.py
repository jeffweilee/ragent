"""BrainCaller Protocol — the contract for a twp-ai-native upstream relay.

Unlike ADKCaller (which parses an ADK-wire SSE stream into UpstreamMessage
objects for ADKAgent to translate), a BrainCaller fronts an upstream that
already emits the twp-ai SSE lifecycle itself (ragent-brain's ``POST /run``).
There is nothing to translate — ``stream_frames`` yields the upstream's SSE
frames verbatim (each a complete ``data: {json}\\n\\n`` block) and BrainAgent
relays them. It raises on transport failure; BrainAgent converts the exception
into a single RUN_ERROR event.

Adding a new twp-ai-native backend:
  1. Create a class with stream_frames() matching the signature below.
  2. Pass it to BrainAgent(caller).
  No changes to the adapter, events, or schemas are needed.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Protocol

from ..schemas import RunAgentInput


class BrainCaller(Protocol):
    """Structural protocol satisfied by any twp-ai-native SSE relay backend.

    stream_frames() yields the upstream's SSE frames unchanged (raw
    ``data: …\\n\\n`` strings). It raises on transport / upstream failure;
    BrainAgent converts the exception into a RUN_ERROR event.
    """

    def stream_frames(
        self,
        request: RunAgentInput,
        model: str,
    ) -> Generator[str, None, None]: ...
