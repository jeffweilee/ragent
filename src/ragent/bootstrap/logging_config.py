"""Structlog configuration: ISO8601 UTC timestamps, OTEL correlation, JSON or console output.

Single entry point ``configure_logging(service)`` sets up:

* structlog processor chain with ISO 8601 UTC timestamps and JSON / console rendering.
* stdlib ``logging`` rerouted through the same chain via ``ProcessorFormatter`` so
  third-party libraries emit consistently formatted records.
* request-scoped contextvars (``service``, plus anything bound by middleware).
* a denylist redaction processor that drops sensitive keys
  (see ``docs/00_rule.md`` Logging Rule).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

import structlog
from opentelemetry import trace

_DENY_KEYS = frozenset(
    {
        "query",
        "prompt",
        "messages",
        "completion",
        "chunks",
        "embedding",
        "documents",
        "body",
        "authorization",
        "cookie",
        "password",
        "token",
        "secret",
    }
)


def _add_otel_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not ctx.is_valid:
        return event_dict
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _drop_denylisted_keys(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    redacted = False
    for key in list(event_dict):
        if key.lower() in _DENY_KEYS:
            del event_dict[key]
            redacted = True
    if redacted:
        event_dict["content_redacted"] = True
    return event_dict


def _build_processor_chain(fmt: str) -> tuple[list, Any]:
    pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_otel_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_denylisted_keys,
        structlog.processors.EventRenamer("message"),
    ]
    if fmt == "console":
        renderer: Any = structlog.dev.ConsoleRenderer(event_key="message")
    else:
        renderer = structlog.processors.JSONRenderer()
    return pre_chain, renderer


def _normalize_iso_timestamp(
    _logger: Any, _name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Convert structlog's ``+00:00`` suffix to ``Z`` for RFC3339 compatibility."""
    ts = event_dict.get("timestamp")
    if isinstance(ts, str) and ts.endswith("+00:00"):
        event_dict["timestamp"] = ts[:-6] + "Z"
    return event_dict


def configure_logging(service: str) -> None:
    """Configure structlog + stdlib logging for the given service.

    Honors ``LOG_LEVEL`` (default ``INFO``) and ``LOG_FORMAT`` (``json`` or ``console``,
    default ``json``).
    """
    fmt = os.getenv("LOG_FORMAT", "json").lower()
    if fmt not in {"json", "console"}:
        fmt = "json"
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    pre_chain, renderer = _build_processor_chain(fmt)
    # Insert the timestamp normalizer right after TimeStamper.
    shared_chain = [
        *pre_chain[:3],
        _normalize_iso_timestamp,
        *pre_chain[3:],
    ]

    structlog.configure(
        processors=[
            *shared_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # Route stdlib logging through the same JSON / console renderer.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_chain,
        processor=renderer,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace existing handlers so we own stdout output.
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Silence uvicorn's default access log; our middleware emits a richer api.request line.
    logging.getLogger("uvicorn.access").disabled = True

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def bound_context() -> Mapping[str, Any]:
    """Return a snapshot of the currently bound contextvars (for tests)."""
    return dict(structlog.contextvars.get_contextvars())
