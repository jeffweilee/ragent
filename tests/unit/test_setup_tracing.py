"""Coverage for bootstrap.telemetry.setup_tracing."""

from __future__ import annotations

import pytest

from ragent.bootstrap.telemetry import setup_tracing


def test_setup_tracing_noop_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Should return without touching OTEL globals.
    setup_tracing("ragent-test")


def test_setup_tracing_wires_provider_when_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "ragent-test-svc")
    monkeypatch.setenv("HAYSTACK_CONTENT_TRACING_ENABLED", "true")

    setup_tracing("ragent-test")

    import haystack.tracing
    from opentelemetry import trace

    # set_tracer_provider replaced the no-op default; round-trip via get_tracer.
    assert trace.get_tracer_provider() is not None
    assert haystack.tracing.tracer.is_content_tracing_enabled is True


def test_setup_tracing_content_tracing_off_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.delenv("HAYSTACK_CONTENT_TRACING_ENABLED", raising=False)

    setup_tracing("ragent-test")

    import haystack.tracing

    assert haystack.tracing.tracer.is_content_tracing_enabled is False
