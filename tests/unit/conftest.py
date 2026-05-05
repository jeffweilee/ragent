"""Shared in-memory OTEL exporter for unit tests.

Uses a single stable TracerProvider (set once before test collection) with a
delegating exporter so module-level ProxyTracer caches are not invalidated
between tests.  Each test that requests ``otel_exporter`` gets a fresh
InMemorySpanExporter swapped into the delegate slot; teardown clears it.
"""

from __future__ import annotations

from collections.abc import Sequence

import opentelemetry.trace as _trace_module
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _DelegatingExporter(SpanExporter):
    """Forwards export calls to a swappable inner exporter."""

    def __init__(self) -> None:
        self._inner: InMemorySpanExporter | None = None

    def swap(self, exporter: InMemorySpanExporter) -> None:
        self._inner = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._inner is not None:
            return self._inner.export(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


_DELEGATING_EXPORTER = _DelegatingExporter()


def _install_stable_provider() -> None:
    """Install a single SDK TracerProvider with our delegating exporter.

    Called once at module import so ProxyTracers created during collection
    bind to this provider and remain valid across all tests.
    """
    exp = _DELEGATING_EXPORTER
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))

    if not isinstance(_trace_module.get_tracer_provider(), TracerProvider):
        # No SDK provider installed yet — set ours.
        _trace_module._TRACER_PROVIDER_SET_ONCE._done = False
        _trace_module._TRACER_PROVIDER = None
        _trace_module.set_tracer_provider(provider)
    else:
        # An SDK provider was already installed (e.g. by a session fixture).
        # Force-replace it so our exporter captures spans.
        _trace_module._TRACER_PROVIDER_SET_ONCE._done = False
        _trace_module._TRACER_PROVIDER = None
        _trace_module.set_tracer_provider(provider)
        # Reset any cached ProxyTracer delegates to pick up the new provider.
        for mod_name in (
            "ragent.clients.llm",
            "ragent.clients.embedding",
            "ragent.clients.rerank",
            "ragent.routers.chat",
            "ragent.routers.retrieve",
        ):
            import sys

            mod = sys.modules.get(mod_name)
            if mod is not None:
                tracer = getattr(mod, "_tracer", None)
                if tracer is not None and hasattr(tracer, "_real_tracer"):
                    tracer._real_tracer = None


_install_stable_provider()


@pytest.fixture()
def otel_exporter():
    inner = InMemorySpanExporter()
    _DELEGATING_EXPORTER.swap(inner)
    yield inner
    inner.clear()
    _DELEGATING_EXPORTER.swap(None)  # type: ignore[arg-type]
