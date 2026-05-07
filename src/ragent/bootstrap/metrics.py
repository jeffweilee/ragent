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
from collections.abc import Callable, Iterable
from functools import lru_cache

import structlog
from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_fastapi_instrumentator import Instrumentator, metrics

_logger = structlog.get_logger(__name__)


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
    "Wall-clock time for the ingest pipeline body, by source_app and mime_type.",
    labelnames=("source_app", "mime_type"),
    buckets=(5, 15, 30, 60, 120, 300, 600),
)


def observe_pipeline_duration(
    *, source_app: str | None, mime_type: str | None, seconds: float
) -> None:
    """Record one ingest-pipeline wall-clock duration sample."""
    worker_pipeline_duration_seconds.labels(
        source_app=normalize_source_app(source_app),
        mime_type=mime_type or _DEFAULT_MIME_LABEL,
    ).observe(seconds)


# Outcome counter — drives the fail-rate query in dashboards:
#   sum by (source_app) (rate(ragent_pipeline_runs_total{outcome="failed"}[5m]))
#   / sum by (source_app) (rate(ragent_pipeline_runs_total[5m]))
_pipeline_runs_total = Counter(
    "ragent_pipeline_runs_total",
    "Ingest pipeline runs grouped by source_app, mime_type, and terminal outcome.",
    labelnames=("source_app", "mime_type", "outcome"),
)

_DEFAULT_MIME_LABEL = "text/plain"


def record_pipeline_outcome(*, source_app: str | None, mime_type: str | None, outcome: str) -> None:
    """Increment ragent_pipeline_runs_total for one terminal pipeline transition.

    `source_app` is normalized through the allow-list to bound cardinality.
    `mime_type` falls back to text/plain (the v2 splitter's documented default
    bucket) when the row predates the mime_type column or when the worker
    couldn't recover it from MinIO HEAD.
    """
    _pipeline_runs_total.labels(
        source_app=normalize_source_app(source_app),
        mime_type=mime_type or _DEFAULT_MIME_LABEL,
        outcome=outcome,
    ).inc()


DocumentStatsRow = tuple[str, str | None, str | None, int]
"""(status, source_app, mime_type, count)."""


def make_document_stats_fetcher(sync_dsn: str) -> Callable[[], list[DocumentStatsRow]]:
    """Build a sync GROUP BY callable for the DocumentStatsCollector.

    A sync engine is used because `collect()` runs on the asyncio event loop
    (the prometheus client scrapes are synchronous), and reaching back into
    aiomysql from there would deadlock. The pool is shared across scrapes —
    Prometheus default scrape interval (15s) leaves plenty of idle time.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(sync_dsn, pool_size=2, pool_pre_ping=True)
    stmt = text(
        "SELECT status, source_app, mime_type, COUNT(*) AS n "
        "FROM documents GROUP BY status, source_app, mime_type"
    )

    def _fetch() -> list[DocumentStatsRow]:
        with engine.connect() as conn:
            return [(row[0], row[1], row[2], int(row[3])) for row in conn.execute(stmt).all()]

    return _fetch


class DocumentStatsCollector:
    """Custom Prometheus collector for `ragent_documents_total`.

    Emits one gauge sample per (status, source_app, mime_type) group, computed
    on-demand at scrape time via the injected `fetch_rows` callable. The
    callable is expected to do `SELECT status, source_app, mime_type, COUNT(*)
    FROM documents GROUP BY 1,2,3` against MariaDB. Injection keeps this class
    DB-free for unit tests; the bootstrap layer wires the real query.

    Errors from `fetch_rows` are logged and swallowed so a transient DB
    blip doesn't 500 the `/metrics` endpoint.
    """

    def __init__(self, fetch_rows: Callable[[], Iterable[DocumentStatsRow]]) -> None:
        self._fetch_rows = fetch_rows

    def collect(self) -> Iterable[GaugeMetricFamily]:
        family = GaugeMetricFamily(
            "ragent_documents_total",
            "Documents by status, source_app, and mime_type.",
            labels=("status", "source_app", "mime_type"),
        )
        try:
            rows = list(self._fetch_rows())
        except Exception as exc:
            _logger.warning(
                "metrics.document_stats_fetch_failed",
                error_code="METRICS_DB_UNAVAILABLE",
                error_type=type(exc).__name__,
            )
            yield family
            return
        for status, source_app, mime_type, count in rows:
            family.add_metric(
                [
                    status,
                    normalize_source_app(source_app),
                    mime_type or _DEFAULT_MIME_LABEL,
                ],
                float(count),
            )
        yield family


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
