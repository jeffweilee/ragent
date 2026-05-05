"""Unit tests for ragent.bootstrap.logging_config."""

from __future__ import annotations

import json
import logging
import re
import sys
from io import StringIO

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider

ISO8601_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


@pytest.fixture(autouse=True)
def _reset_structlog():
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _capture_stdout_log(monkeypatch, fmt: str = "json") -> StringIO:
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("LOG_FORMAT", fmt)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    from ragent.bootstrap.logging_config import configure_logging

    configure_logging("ragent-test")
    return buf


def _last_json_line(buf: StringIO) -> dict:
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert lines, f"no log lines emitted; buf={buf.getvalue()!r}"
    return json.loads(lines[-1])


def test_timestamp_is_iso8601_utc(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    structlog.get_logger("t").info("hello")
    record = _last_json_line(buf)
    assert "timestamp" in record
    assert ISO8601_UTC.match(record["timestamp"]), record["timestamp"]


def test_service_name_bound(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    structlog.get_logger("t").info("hello")
    assert _last_json_line(buf)["service"] == "ragent-test"


def test_event_renamed_to_message(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    structlog.get_logger("t").info("api.request", method="GET")
    record = _last_json_line(buf)
    assert record.get("message") == "api.request"
    assert "event" not in record


def test_log_level_field_present(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    structlog.get_logger("t").warning("warn-event")
    assert _last_json_line(buf)["level"] == "warning"


def test_otel_trace_id_injected(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("s1"):
        structlog.get_logger("t").info("inside-span")
    record = _last_json_line(buf)
    assert "trace_id" in record
    assert "span_id" in record
    assert re.fullmatch(r"[0-9a-f]{32}", record["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", record["span_id"])


def test_no_trace_id_when_no_span(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    structlog.get_logger("t").info("no-span")
    record = _last_json_line(buf)
    assert "trace_id" not in record
    assert "span_id" not in record


def test_exception_renders_with_traceback(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    log = structlog.get_logger("t")
    try:
        raise ValueError("boom-marker")
    except ValueError:
        log.exception("api.error")
    record = _last_json_line(buf)
    assert record["message"] == "api.error"
    assert "exception" in record
    assert "ValueError" in record["exception"]


def test_stdlib_logging_routed_through_structlog(monkeypatch):
    buf = _capture_stdout_log(monkeypatch)
    logging.getLogger("legacy.module").info("legacy-msg")
    record = _last_json_line(buf)
    assert record.get("message") == "legacy-msg"
    assert record["service"] == "ragent-test"


def test_console_format_does_not_emit_json(monkeypatch):
    buf = _capture_stdout_log(monkeypatch, fmt="console")
    structlog.get_logger("t").info("hello", k="v")
    out = buf.getvalue()
    # Console renderer is not JSON; first non-empty line must not be JSON-parseable.
    line = next(line for line in out.splitlines() if line.strip())
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "hello" in out
