"""T-BRAIN.3 — BrainAgent (relay a twp-ai-native upstream; frame transport errors)."""

from __future__ import annotations

import json
from collections.abc import Generator

from twp_ai.agents.brain import BrainAgent
from twp_ai.schemas import RunAgentInput

from ragent.errors.codes import HttpErrorCode
from ragent.errors.upstream import UpstreamServiceError


def _request() -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread_1",
            "runId": "run_1",
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "tools": [],
            "state": None,
            "context": [],
            "forwardedProps": None,
        }
    )


class _FakeCaller:
    def __init__(self, frames: list[str], raise_after: int | None = None) -> None:
        self._frames = frames
        self._raise_after = raise_after

    def stream_frames(self, request: RunAgentInput, model: str) -> Generator[str, None, None]:
        for i, frame in enumerate(self._frames):
            if self._raise_after is not None and i == self._raise_after:
                raise UpstreamServiceError(
                    "brain down",
                    service="brain",
                    error_code=HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR,
                )
            yield frame
        if self._raise_after is not None and self._raise_after >= len(self._frames):
            raise UpstreamServiceError(
                "brain down", service="brain", error_code=HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR
            )


def _events(frames: list[str]) -> list[dict]:
    return [json.loads(f.removeprefix("data: ").strip()) for f in frames]


def test_relays_frames_unchanged_no_extra_envelope() -> None:
    frames = [
        'data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}\n\n',
        'data: {"type":"RUN_FINISHED","runId":"run_1","threadId":"thread_1"}\n\n',
    ]
    out = list(BrainAgent(_FakeCaller(frames)).run(_request(), ""))
    # Byte-identical passthrough; BrainAgent adds no RUN_STARTED/RUN_FINISHED.
    assert out == frames


def test_transport_failure_before_first_frame_yields_lone_run_error() -> None:
    out = list(BrainAgent(_FakeCaller([], raise_after=0)).run(_request(), ""))
    events = _events(out)
    assert len(events) == 1
    assert events[0]["type"] == "RUN_ERROR"
    assert events[0]["code"] == HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR


def test_transport_failure_mid_stream_appends_terminal_run_error() -> None:
    frames = ['data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}\n\n']
    out = list(BrainAgent(_FakeCaller(frames, raise_after=1)).run(_request(), ""))
    events = _events(out)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR
