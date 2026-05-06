"""T7.1 — OTEL tracing setup and Prometheus metrics registry (B28)."""

from __future__ import annotations

import os

# Haystack's module-level default is True; enforce privacy-by-default before
# any haystack import triggers the PostHog initialisation check.
os.environ.setdefault("HAYSTACK_TELEMETRY_ENABLED", "false")

from prometheus_client import Counter, Histogram

reconciler_tick_total = Counter(
    "reconciler_tick_total",
    "Number of reconciler ticks executed",
)

minio_orphan_object_total = Counter(
    "minio_orphan_object_total",
    "Number of MinIO objects orphaned after terminal status commit",
)

multi_ready_repaired_total = Counter(
    "multi_ready_repaired_total",
    "Number of multi-READY groups repaired by reconciler",
)

worker_pipeline_duration_seconds = Histogram(
    "worker_pipeline_duration_seconds",
    "Wall-clock time for the ingest pipeline body",
    buckets=(5, 15, 30, 60, 120, 300, 600),
)


def setup_tracing(service_name: str) -> None:
    """Configure OTLP tracing if OTEL_EXPORTER_OTLP_ENDPOINT is set; no-op otherwise."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    import haystack.tracing
    from haystack.tracing.opentelemetry import OpenTelemetryTracer

    haystack.tracing.tracer.actual_tracer = OpenTelemetryTracer(trace.get_tracer(service_name))
    # Pin content tracing off by default to keep prompts / answers out of spans.
    # Override only when HAYSTACK_CONTENT_TRACING_ENABLED is explicitly truthy.
    enable_content = os.environ.get("HAYSTACK_CONTENT_TRACING_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )
    haystack.tracing.tracer.is_content_tracing_enabled = enable_content
