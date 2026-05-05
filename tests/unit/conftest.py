"""Shared in-memory OTEL exporter for unit tests.

Sets the global TracerProvider exactly once (OTEL refuses to override) and
exposes a session-scoped ``otel_exporter`` fixture that any test can use to
inspect spans produced by the production code under test.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_PROVIDER = TracerProvider()
_EXPORTER = InMemorySpanExporter()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture()
def otel_exporter():
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()
