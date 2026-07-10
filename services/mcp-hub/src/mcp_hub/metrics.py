"""Prometheus metrics for the MCP Hub.

Metric names are intentionally identical to those in ragent.bootstrap.metrics
so existing Grafana dashboards and alert rules work without modification
when the hub runs as a standalone service.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

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
