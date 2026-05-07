"""Prometheus metrics registry — business + infrastructure metrics.

Metric *definitions* live here so that import-time side effects (registering on
the default `prometheus_client.REGISTRY`) happen exactly once, regardless of
which subsystem (API, worker, reconciler) imports them. Tracing setup remains
in `bootstrap.telemetry`.
"""

from __future__ import annotations

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
