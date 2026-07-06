"""BrainAgent — agent for a twp-ai-native upstream (ragent-brain).

Use this when the upstream service already emits the twp-ai SSE lifecycle
itself (``RUN_STARTED`` → ``TEXT_/REASONING_/TOOL_/STATE_*`` →
``RUN_FINISHED{success|interrupt}`` / ``RUN_ERROR``). Unlike ADKAgent — which
maps an ADK-wire delta stream onto twp-ai events and OWNS the run envelope —
BrainAgent is a **relay**: the upstream already brackets the run, so BrainAgent
passes every frame through unchanged and never emits its own
``RUN_STARTED``/``RUN_FINISHED`` (that would duplicate the lifecycle).

BrainAgent synthesizes exactly one event, and only on **transport failure**:
a ``RUN_ERROR``. If the caller raises before the first frame (upstream
unreachable / timeout) the run is a lone ``RUN_ERROR``; if it raises mid-stream
(connection dropped after some frames) the already-relayed frames are followed
by a terminal ``RUN_ERROR`` so the stream always terminates. Once the upstream
has emitted its own terminal frame, that frame is the run's terminal frame.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from ..callers.brain import BrainCaller
from ..events import to_sse
from ..schemas import RunAgentInput
from ._run_error import run_error_event

logger = logging.getLogger(__name__)


class BrainAgent:
    """Relays a twp-ai-native upstream's SSE frames, framing only transport errors."""

    def __init__(self, caller: BrainCaller) -> None:
        self._caller = caller

    def run(self, request: RunAgentInput, model: str) -> Generator[str, None, None]:
        try:
            yield from self._caller.stream_frames(request, model)
        except Exception as exc:
            yield to_sse(
                run_error_event(
                    exc,
                    run_id=request.run_id,
                    thread_id=request.thread_id or "",
                    logger=logger,
                )
            )
