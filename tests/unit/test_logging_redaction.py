"""Privacy guardrail: denylisted keys must never reach the rendered log."""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset():
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _capture(monkeypatch) -> StringIO:
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    from ragent.bootstrap.logging_config import configure_logging

    configure_logging("ragent-test")
    return buf


def _last(buf: StringIO) -> dict:
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return json.loads(lines[-1])


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
def test_denylisted_key_is_dropped(monkeypatch, key):
    buf = _capture(monkeypatch)
    structlog.get_logger("t").info("evt", **{key: "supersecret-value"})
    record = _last(buf)
    assert key not in record
    assert "supersecret-value" not in json.dumps(record)
    assert record.get("content_redacted") is True


def test_safe_keys_are_preserved(monkeypatch):
    buf = _capture(monkeypatch)
    structlog.get_logger("t").info(
        "api.request",
        method="POST",
        path="/chat",
        status_code=200,
        duration_ms=12.3,
        request_id="req-1",
        user_id="u1",
        query_len=42,
        result_count=3,
    )
    record = _last(buf)
    assert record["method"] == "POST"
    assert record["status_code"] == 200
    assert record["query_len"] == 42
    assert "content_redacted" not in record


def test_denylist_is_case_insensitive(monkeypatch):
    buf = _capture(monkeypatch)
    structlog.get_logger("t").info("evt", Authorization="Bearer xyz")
    record = _last(buf)
    assert "Authorization" not in record
    assert record.get("content_redacted") is True
