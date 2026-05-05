"""Shared in-memory OTEL exporter for unit tests.

Attaches an in-memory exporter to the current global TracerProvider so spans
produced by production code under test become inspectable, regardless of
whether another test (or production setup) installed the provider first.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_EXPORTER = InMemorySpanExporter()


def _attach_exporter() -> None:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))


_attach_exporter()


@pytest.fixture()
def otel_exporter():
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()
