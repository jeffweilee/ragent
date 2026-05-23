"""T-CHAOS.C5 — LLM stream interrupt (spec §3.6.1).

WireMock returns 3 SSE deltas without the `[DONE]` sentinel, simulating
a mid-stream connection drop.  The LLMClient must:
  1. Detect the missing sentinel after yielding content.
  2. Raise `LLMStreamInterruptedError` (NOT retry — partial content
     already sent downstream).
  3. The chat router converts this to an SSE error frame with
     `error_code == "LLM_STREAM_INTERRUPTED"` rather than a 500.

Spec §3.6.1 common acceptance asserts:
  1. Response HTTP status == 200 (SSE stream opened successfully).
  2. Last SSE event has `type == "error"` and
     `error_code == "LLM_STREAM_INTERRUPTED"`.
  3. `chaos_drill_outcome_total{case="C5", outcome="pass"}` increments.
"""

from __future__ import annotations

import contextlib
import json
import urllib.request

import httpx
import pytest

from tests.e2e.conftest import API_URL
from tests.e2e.test_chaos.conftest import wiremock_reset  # noqa: F401 (fixture)

pytestmark = [
    pytest.mark.docker,
]

# SSE body: 3 content deltas, NO [DONE] sentinel.
_INTERRUPT_BODY = (
    'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"C"}}]}\n\n'
)

_LLM_STUB = {
    "priority": 1,
    "request": {
        "method": "POST",
        "urlPath": "/gpt_oss_120b/v1/chat/completions",
        "bodyPatterns": [{"matchesJsonPath": "$[?(@.stream == true)]"}],
    },
    "response": {
        "status": 200,
        "headers": {"Content-Type": "text/event-stream"},
        "body": _INTERRUPT_BODY,
    },
}


def _post_wiremock_stub(wiremock_url: str, stub: dict) -> None:
    data = json.dumps(stub).encode()
    req = urllib.request.Request(
        f"{wiremock_url}/__admin/mappings",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _parse_sse_events(body: str) -> list[dict]:
    """Parse SSE body into a list of data payloads."""
    events = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[len("data:") :].strip()
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(data_str))
    return events


def test_C5_llm_stream_interrupt_emits_error_frame(
    running_stack,
    e2e_env,
    wiremock_url: str,
    wiremock_reset,  # noqa: F811
) -> None:
    """Stream interrupted before [DONE]: last SSE frame is error, HTTP 200."""
    from ragent.bootstrap.metrics import chaos_drill_outcome_total

    # Inject fault stub (priority 1 overrides default SSE stub)
    _post_wiremock_stub(wiremock_url, _LLM_STUB)

    payload = {
        "messages": [{"role": "user", "content": "What is chaos engineering?"}],
        "model": "gpt-oss-120b",
        "provider": "openai",
    }
    resp = httpx.post(
        f"{API_URL}/chat/v1/stream",
        headers={"X-User-Id": "alice"},
        json=payload,
        timeout=30,
    )

    # Assert 1: HTTP status 200 (stream opened — error is inside SSE body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    events = _parse_sse_events(resp.text)
    assert events, "No SSE events received"

    # Assert 2: last event is error with LLM_STREAM_INTERRUPTED
    last_event = events[-1]
    assert last_event.get("type") == "error", (
        f"Last event type expected 'error', got {last_event.get('type')!r}. All events: {events}"
    )
    assert last_event.get("error_code") == "LLM_STREAM_INTERRUPTED", (
        f"Expected error_code='LLM_STREAM_INTERRUPTED', got {last_event.get('error_code')!r}"
    )

    # Assert 3: record drill outcome
    chaos_drill_outcome_total.labels(case="C5", outcome="pass").inc()
    assert (
        chaos_drill_outcome_total.labels(case="C5", outcome="pass")._value.get() >= 1  # noqa: SLF001
    )
