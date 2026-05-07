"""Prometheus metrics registry — business + infrastructure metrics.

Metric *definitions* live here so that import-time side effects (registering on
the default `prometheus_client.REGISTRY`) happen exactly once, regardless of
which subsystem (API, worker, reconciler) imports them. Tracing setup remains
in `bootstrap.telemetry`.

`setup_metrics(app)` wires `prometheus-fastapi-instrumentator` for HTTP-layer
metrics and exposes `/metrics` from the same app. Health/metrics paths are
excluded from HTTP tracking so probe traffic doesn't drown real RPS.
"""

from __future__ import annotations

import os
from functools import lru_cache

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator, metrics


@lru_cache(maxsize=1)
def _source_app_allowlist() -> frozenset[str]:
    raw = os.environ.get("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "")
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


@lru_cache(maxsize=1)
def _source_app_fallback() -> str:
    return os.environ.get("RAGENT_METRICS_SOURCE_APP_FALLBACK", "other")


def normalize_source_app(raw: str | None) -> str:
    """Map raw `source_app` to a bounded label value.

    Values in `RAGENT_METRICS_SOURCE_APP_ALLOWLIST` pass through verbatim;
    everything else collapses to `RAGENT_METRICS_SOURCE_APP_FALLBACK`
    (default `"other"`) to keep label cardinality bounded.
    """
    if raw and raw in _source_app_allowlist():
        return raw
    return _source_app_fallback()


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


# Paths excluded from HTTP request metrics. Anchored regexes — must match the
# full path so a future `/metrics-foo` route would still be tracked.
_EXCLUDED_HANDLERS = (
    r"^/metrics$",
    r"^/livez$",
    r"^/readyz$",
    r"^/startupz$",
    r"^/docs$",
    r"^/redoc$",
    r"^/openapi\.json$",
)


# Built once on first call so re-binding to a fresh FastAPI app (e.g. in tests)
# doesn't try to re-create the same Counter/Histogram on the global registry.
_HTTP_METRIC_FNS: list = []


def _http_metric_fns() -> list:
    if _HTTP_METRIC_FNS:
        return _HTTP_METRIC_FNS
    _HTTP_METRIC_FNS.extend(
        fn
        for fn in (
            metrics.default(),
            metrics.latency(buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)),
            metrics.request_size(),
            metrics.response_size(),
        )
        if fn is not None
    )
    return _HTTP_METRIC_FNS


def setup_metrics(app: FastAPI) -> Instrumentator:
    """Register HTTP metrics + expose `/metrics` on `app`.

    Auth bypass: `/metrics` is in `_NO_USER_ID_PATHS` (bootstrap/app.py), so
    requests reach the instrumentator without an `X-User-Id` header.
    """
    inst = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_group_untemplated=True,
        excluded_handlers=list(_EXCLUDED_HANDLERS),
        inprogress_name="http_requests_inprogress",
        inprogress_labels=True,
    )
    for fn in _http_metric_fns():
        inst.add(fn)
    inst.instrument(app)
    inst.expose(app, endpoint="/metrics", include_in_schema=False)
    return inst
