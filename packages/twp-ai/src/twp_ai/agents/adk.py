"""ADKAgent — agent for an upstream agent service that streams text deltas.

Use this when the agent logic lives in an external service (an "ADK"-style
upstream) and ragent only proxies it. The upstream owns its own tool loop;
this agent does not build a system prompt or manage tools — it relays the
upstream's assistant text as the twp-ai text lifecycle:

    RUN_STARTED
    TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT* / TEXT_MESSAGE_END
    RUN_FINISHED

Any exception raised by the caller (transport failure, upstream error) is
surfaced as a single RUN_ERROR event. The error code is taken from the
exception's `error_code` attribute when present (ragent domain-exception
convention), otherwise the exception class name.

To support a different upstream flow, write a new Agent class rather than
subclassing this one.
"""

from __future__ import annotations

from collections.abc import Generator

from .._compose import Turn
from ..callers.adk import ADKCaller
from ..events import RunErrorEvent, RunFinishedEvent, RunStartedEvent, to_sse
from ..schemas import RunAgentInput


class ADKAgent:
    """Relays an upstream text stream as twp-ai events."""

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
            deltas = (("text", delta) for delta in self._caller.stream_deltas(request, model))
            yield from Turn(deltas)
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
