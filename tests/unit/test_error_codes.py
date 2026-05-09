"""Phase H — error-code centralization (00_rule.md §API Error Honesty).

Pins three guarantees so future drift fails loudly:

1. Each StrEnum member compares equal to its raw string value (back-compat
   with string-literal call sites and JSON serialization).
2. ``PIPELINE_TIMEOUT`` (the dishonestly named legacy code) does NOT exist
   — its replacement is ``PIPELINE_UNEXPECTED_ERROR``.
3. The three surfaces (HTTP / Task / Probe) stay disjoint by intent: any
   future code-name shared across them is a contract change, not a typo.
"""

from __future__ import annotations

from ragent.errors.codes import HttpErrorCode, ProbeErrorCode, TaskErrorCode


def test_strenum_compares_equal_to_raw_string():
    assert HttpErrorCode.LLM_ERROR == "LLM_ERROR"
    assert TaskErrorCode.PIPELINE_UNEXPECTED_ERROR == "PIPELINE_UNEXPECTED_ERROR"
    assert ProbeErrorCode.PROBE_TIMEOUT == "PROBE_TIMEOUT"


def test_strenum_serializes_as_raw_string():
    """Critical for problem-details JSON — the response body MUST contain
    the bare code string, not the enum's repr."""
    import json

    payload = {"error_code": HttpErrorCode.EMBEDDER_TIMEOUT}
    assert json.loads(json.dumps(payload))["error_code"] == "EMBEDDER_TIMEOUT"


def test_legacy_pipeline_timeout_renamed():
    """PIPELINE_TIMEOUT was misnamed (fired on any non-IngestStepError, not
    only timeouts). Renamed to PIPELINE_UNEXPECTED_ERROR; legacy name
    must not reappear via copy-paste."""
    assert "PIPELINE_TIMEOUT" not in {m.value for m in TaskErrorCode}
    assert TaskErrorCode.PIPELINE_UNEXPECTED_ERROR.value == "PIPELINE_UNEXPECTED_ERROR"
    assert TaskErrorCode.PIPELINE_TIMEOUT_AGGREGATE.value == "PIPELINE_TIMEOUT_AGGREGATE"


def test_surfaces_are_disjoint_by_intent():
    """A code crossing surfaces is a contract change — pin the current set."""
    http_codes = {m.value for m in HttpErrorCode}
    task_codes = {m.value for m in TaskErrorCode}
    probe_codes = {m.value for m in ProbeErrorCode}

    # EMBEDDER_ERROR appears on both Http (502 from /chat) and Task (worker
    # writes to documents.error_code) surfaces — same root cause, two
    # observables. Other codes are surface-exclusive.
    expected_overlap = {"EMBEDDER_ERROR"}
    assert (http_codes & task_codes) == expected_overlap
    assert (http_codes & probe_codes) == set()
    assert (task_codes & probe_codes) == set()
