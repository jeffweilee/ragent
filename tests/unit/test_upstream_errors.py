"""Phase A — Typed upstream-service exceptions (00_rule.md §API Error Honesty)."""

from __future__ import annotations

from ragent.errors.upstream import UpstreamServiceError, UpstreamTimeoutError


def test_upstream_service_error_default_attributes() -> None:
    exc = UpstreamServiceError("embed boom", service="embedding")
    assert exc.service == "embedding"
    assert exc.error_code == "UPSTREAM_ERROR"
    assert exc.http_status == 502
    assert "embed boom" in str(exc)


def test_upstream_service_error_custom_error_code() -> None:
    exc = UpstreamServiceError("boom", service="embedding", error_code="EMBEDDER_ERROR")
    assert exc.error_code == "EMBEDDER_ERROR"
    assert exc.http_status == 502


def test_upstream_timeout_error_defaults_to_504() -> None:
    exc = UpstreamTimeoutError("slow", service="llm")
    assert exc.http_status == 504
    assert exc.error_code == "UPSTREAM_TIMEOUT"
    assert isinstance(exc, UpstreamServiceError)


def test_upstream_timeout_error_custom_error_code() -> None:
    exc = UpstreamTimeoutError("slow", service="llm", error_code="LLM_TIMEOUT")
    assert exc.http_status == 504
    assert exc.error_code == "LLM_TIMEOUT"


def test_chained_cause_preserved() -> None:
    inner = ValueError("root cause")
    try:
        raise UpstreamServiceError("wrapped", service="rerank") from inner
    except UpstreamServiceError as exc:
        assert exc.__cause__ is inner
