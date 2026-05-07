"""Coverage for bootstrap.telemetry.setup_tracing.

The OTLP path mutates global OTEL state (`trace.set_tracer_provider`,
`haystack.tracing.tracer.actual_tracer`) which other unit tests depend on
via `tests/unit/conftest.py`'s stable provider install. To keep this test
side-effect-free we mock the OTEL/Haystack collaborators and assert the
function reaches them, without actually swapping providers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ragent.bootstrap.telemetry import setup_tracing


def test_setup_tracing_noop_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # No OTEL imports should be triggered. patch.object guards against future
    # accidental imports inside the no-op branch.
    with patch("opentelemetry.trace.set_tracer_provider") as set_provider:
        setup_tracing("ragent-test")
    set_provider.assert_not_called()


def test_setup_tracing_calls_set_provider_when_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "ragent-test-svc")
    monkeypatch.setenv("HAYSTACK_CONTENT_TRACING_ENABLED", "true")

    with (
        patch("opentelemetry.trace.set_tracer_provider") as set_provider,
        patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"),
        patch("haystack.tracing.tracer") as haystack_tracer,
    ):
        setup_tracing("ragent-test")

    set_provider.assert_called_once()
    assert haystack_tracer.is_content_tracing_enabled is True


def test_setup_tracing_content_tracing_off_when_flag_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.delenv("HAYSTACK_CONTENT_TRACING_ENABLED", raising=False)

    with (
        patch("opentelemetry.trace.set_tracer_provider"),
        patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"),
        patch("haystack.tracing.tracer") as haystack_tracer,
    ):
        setup_tracing("ragent-test")

    assert haystack_tracer.is_content_tracing_enabled is False
