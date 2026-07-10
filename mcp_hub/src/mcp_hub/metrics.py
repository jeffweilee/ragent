"""Prometheus metrics for the MCP Hub.

Metric names are intentionally identical to those in ragent.bootstrap.metrics
so existing Grafana dashboards and alert rules work without modification
when the hub runs as a standalone service.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_MCP_HUB_LOAD_PHASES = frozenset({"file_parse", "tool_parse", "registration"})
_MCP_HUB_CALL_OUTCOMES = frozenset(
    {"success", "upstream_4xx", "upstream_5xx", "timeout", "connect_error"}
)

mcp_hub_tool_load_failures_total = Counter(
    "mcp_hub_tool_load_failures_total",
    "Startup-time load failures by system and phase.",
    labelnames=("system", "phase"),
)

mcp_hub_tool_calls_total = Counter(
    "mcp_hub_tool_calls_total",
    "Tool invocations by system, tool name, and outcome.",
    labelnames=("system", "tool", "outcome"),
)

# `tool` deliberately omitted from this histogram's labels so le-bucket
# cardinality stays bounded; the counter above keeps `tool` for drill-down.
mcp_hub_tool_call_duration_seconds = Histogram(
    "mcp_hub_tool_call_duration_seconds",
    "Wall-clock duration of upstream call per system, by outcome.",
    labelnames=("system", "outcome"),
)

# Inventory gauge: one time-series per registered tool, value always 1.
# Solves the zero-cardinality gap — tools that are never called still appear
# in Prometheus so dashboards and alerts can verify the expected tool set.
mcp_hub_tool_info = Gauge(
    "mcp_hub_tool_info",
    "Registered tool inventory; value is always 1. "
    "Exposes all tools at startup so alert rules can detect missing tools "
    "even before the first call.",
    labelnames=("system", "tool", "method"),
)

# Per-system liveness gauge: 1 = loaded successfully, 0 = skipped due to
# load failure. Resets on every restart; pairs with
# mcp_hub_tool_load_failures_total for a complete startup-health picture.
mcp_hub_system_up = Gauge(
    "mcp_hub_system_up",
    "1 if the system loaded successfully at startup, 0 if skipped due to load failure.",
    labelnames=("system",),
)


def record_mcp_hub_load_failure(*, system: str, phase: str) -> None:
    if phase not in _MCP_HUB_LOAD_PHASES:
        raise ValueError(f"unknown mcp_hub load phase {phase!r}")
    mcp_hub_tool_load_failures_total.labels(system=system or "unknown", phase=phase).inc()


def record_mcp_hub_tool_call(
    *, system: str, tool: str, outcome: str, duration_seconds: float
) -> None:
    if outcome not in _MCP_HUB_CALL_OUTCOMES:
        raise ValueError(f"unknown mcp_hub call outcome {outcome!r}")
    mcp_hub_tool_calls_total.labels(system=system, tool=tool, outcome=outcome).inc()
    mcp_hub_tool_call_duration_seconds.labels(system=system, outcome=outcome).observe(
        duration_seconds
    )


def record_mcp_hub_tool_registered(*, system: str, tool: str, method: str) -> None:
    mcp_hub_tool_info.labels(system=system, tool=tool, method=method).set(1)


def record_mcp_hub_system_up(*, system: str, up: bool) -> None:
    mcp_hub_system_up.labels(system=system).set(1 if up else 0)
