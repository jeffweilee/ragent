"""Hub-level Prometheus counters/histograms + the verify_ssl per-system knob.

Three signal families:
- `mcp_hub_tool_load_failures_total{system, phase}` — startup-time yaml /
  registration failures, phased so dashboards can distinguish "bad file"
  from "bad tool" from "FastMCP add_tool rejection".
- `mcp_hub_tool_calls_total{system, tool, outcome}` — every tool
  invocation, outcome in a closed enum.
- `mcp_hub_tool_call_duration_seconds{system, outcome}` — histogram of
  upstream call latency. `tool` is deliberately dropped from this
  metric's labels to keep le-bucket cardinality bounded; the counter
  retains it for drill-down.

Plus a per-system `verify_ssl: bool` knob in `defaults` that flows into
the system's `httpx.AsyncClient(verify=...)`.
"""

from __future__ import annotations

import asyncio
import ssl
import textwrap
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from ragent.bootstrap.metrics import (
    record_mcp_hub_load_failure,
    record_mcp_hub_tool_call,
)
from ragent.mcp_hub.mcp_hub import (
    LoadFailure,
    _SystemSpec,
    build_hub,
    load_tools_yaml,
)


def _counter(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_record_tool_call_success_increments_counter_and_histogram():
    before_calls = _counter(
        "mcp_hub_tool_calls_total",
        {"system": "billing", "tool": "billing.list", "outcome": "success"},
    )
    before_hist = (
        REGISTRY.get_sample_value(
            "mcp_hub_tool_call_duration_seconds_count",
            {"system": "billing", "outcome": "success"},
        )
        or 0.0
    )

    record_mcp_hub_tool_call(
        system="billing", tool="billing.list", outcome="success", duration_seconds=0.123
    )

    assert (
        _counter(
            "mcp_hub_tool_calls_total",
            {"system": "billing", "tool": "billing.list", "outcome": "success"},
        )
        == before_calls + 1
    )
    after_hist = REGISTRY.get_sample_value(
        "mcp_hub_tool_call_duration_seconds_count",
        {"system": "billing", "outcome": "success"},
    )
    assert after_hist == before_hist + 1


@pytest.mark.parametrize("outcome", ["upstream_4xx", "upstream_5xx", "timeout", "connect_error"])
def test_record_tool_call_failure_outcomes(outcome: str):
    before = _counter(
        "mcp_hub_tool_calls_total",
        {"system": "identity", "tool": "identity.x", "outcome": outcome},
    )
    record_mcp_hub_tool_call(
        system="identity", tool="identity.x", outcome=outcome, duration_seconds=0.01
    )
    assert (
        _counter(
            "mcp_hub_tool_calls_total",
            {"system": "identity", "tool": "identity.x", "outcome": outcome},
        )
        == before + 1
    )


def test_record_tool_call_unknown_outcome_raises():
    """Closed-enum guard matches `record_ingest_rejection` — caller typo
    fails fast in tests, never blow up label cardinality at runtime."""
    with pytest.raises(ValueError, match="unknown mcp_hub call outcome"):
        record_mcp_hub_tool_call(
            system="x", tool="x.y", outcome="totally-invalid", duration_seconds=0.001
        )


@pytest.mark.parametrize("phase", ["file_parse", "tool_parse", "registration"])
def test_record_load_failure_phases(phase: str):
    before = _counter("mcp_hub_tool_load_failures_total", {"system": "billing", "phase": phase})
    record_mcp_hub_load_failure(system="billing", phase=phase)
    assert (
        _counter("mcp_hub_tool_load_failures_total", {"system": "billing", "phase": phase})
        == before + 1
    )


def test_record_load_failure_unknown_phase_raises():
    with pytest.raises(ValueError, match="unknown mcp_hub load phase"):
        record_mcp_hub_load_failure(system="billing", phase="not-a-phase")


def test_load_failure_carries_system_phase_tool_fields():
    """LoadFailure exposes structured fields so `mcp_hub.load_failure` logs
    and the load-failure counter can drill down by (system, phase) without
    parsing the free-form `reason` string."""
    f = LoadFailure(source="billing.yaml:create_invoice", reason="bad schema")
    # defaults present so callers don't break
    assert f.system == ""
    assert f.phase == "tool_parse"
    assert f.tool == ""

    f2 = LoadFailure(
        source="billing.yaml:create_invoice",
        reason="bad schema",
        system="billing",
        phase="tool_parse",
        tool="create_invoice",
    )
    assert (f2.system, f2.phase, f2.tool) == ("billing", "tool_parse", "create_invoice")


@pytest.mark.parametrize(
    "yaml_value",
    [
        '"false"',  # string — bool('false') would be True (silently leave TLS on)
        '"true"',  # string
        "null",  # bool(None) would be False (silently disable TLS!)
        "''",  # empty string — bool('') would be False
        "0",  # int — bool(0) would be False
        "1",  # int — bool(1) would be True
    ],
)
def test_system_spec_verify_ssl_rejects_non_boolean(tmp_path: Path, yaml_value: str):
    """`verify_ssl` controls TLS verification; permissive `bool(...)` coercion
    would let a yaml typo (`"false"`, `null`, `0`, ...) flip the security
    setting unexpectedly. Loader must reject anything that is not an actual
    yaml boolean."""
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            f"""\
            system: bad
            defaults:
              base_url: https://api.example.com
              timeout: 5
              verify_ssl: {yaml_value}
            tools:
              - name: ping
                method: GET
                path: /ping
            """
        )
    )
    result = load_tools_yaml(yaml_file, strict=False)
    assert "bad" not in result.systems, f"non-bool verify_ssl={yaml_value} must be rejected"
    assert any("verify_ssl" in f.reason and "boolean" in f.reason for f in result.failures), (
        f"expected explicit verify_ssl rejection, got {[f.reason for f in result.failures]}"
    )


def test_system_spec_verify_ssl_defaults_true_and_can_be_disabled(tmp_path: Path):
    yaml_file = tmp_path / "ok.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """\
            system: ok
            defaults:
              base_url: https://api.example.com
              timeout: 5
            tools:
              - name: ping
                method: GET
                path: /ping
            """
        )
    )
    result = load_tools_yaml(yaml_file)
    assert result.systems["ok"].verify_ssl is True

    yaml_file = tmp_path / "insecure.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """\
            system: insecure
            defaults:
              base_url: https://internal.example.com
              timeout: 5
              verify_ssl: false
            tools:
              - name: ping
                method: GET
                path: /ping
            """
        )
    )
    result = load_tools_yaml(yaml_file)
    assert result.systems["insecure"].verify_ssl is False


def test_make_client_passes_verify_through_to_httpx():
    """`verify_ssl=False` flows into the underlying httpx SSLContext —
    verify_mode=CERT_NONE (0) + check_hostname=False."""
    insecure = _SystemSpec(
        name="t",
        base_url="https://internal.example.com",
        timeout=5.0,
        max_connections=10,
        default_headers={},
        source=Path("/tmp/x.yaml"),
        verify_ssl=False,
    )
    secure = _SystemSpec(
        name="t",
        base_url="https://api.example.com",
        timeout=5.0,
        max_connections=10,
        default_headers={},
        source=Path("/tmp/y.yaml"),
        verify_ssl=True,
    )
    cli_off = insecure.make_client()
    cli_on = secure.make_client()
    try:
        ctx_off = cli_off._transport._pool._ssl_context
        ctx_on = cli_on._transport._pool._ssl_context
        assert ctx_off.verify_mode == ssl.CERT_NONE and ctx_off.check_hostname is False
        assert ctx_on.verify_mode == ssl.CERT_REQUIRED and ctx_on.check_hostname is True
    finally:
        asyncio.run(cli_off.aclose())
        asyncio.run(cli_on.aclose())


@pytest.mark.asyncio
async def test_build_hub_emits_load_failure_with_structured_fields_and_increments_counter(
    tmp_path: Path,
):
    """Uses `structlog.testing.capture_logs` (NOT `caplog` + stdlib bridge)
    because the stdlib route flakes in CI's pytest-cov instrumented run
    even though it passes locally — see docs/00_journal.md row
    2026-05-20 'Log Capture Bridge'."""
    bad = tmp_path / "broken.yaml"
    bad.write_text(
        textwrap.dedent(
            """\
            system: broken
            defaults:
              base_url: https://api.example.com
              timeout: 5
            tools:
              - name: bad
                method: GET
                path: /x
                parameters:
                  - name: arg
                    type: not-a-type
                    location: query
            """
        )
    )

    before = _counter(
        "mcp_hub_tool_load_failures_total",
        {"system": "broken", "phase": "tool_parse"},
    )

    with capture_logs() as captured:
        bundle = build_hub(tmp_path)

    try:
        load_fails = [e for e in captured if e.get("event") == "mcp_hub.load_failure"]
        assert any(
            e.get("system") == "broken"
            and e.get("phase") == "tool_parse"
            and e.get("tool") == "bad"
            for e in load_fails
        ), f"missing structured load_failure fields; got {load_fails}"
    finally:
        for c in bundle.clients.values():
            await c.aclose()

    assert (
        _counter(
            "mcp_hub_tool_load_failures_total",
            {"system": "broken", "phase": "tool_parse"},
        )
        == before + 1
    )
