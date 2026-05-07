"""DocumentStatsCollector — emits ragent_documents_total at scrape time.

Single metric `ragent_documents_total{status, source_app, mime_type}` so a
dashboard can aggregate via `sum by (status)`, `sum by (source_app, status)`,
etc. without us shipping multiple pre-aggregated metrics.

The collector receives an injected callable (`fetch_rows`) that does the
GROUP BY against MariaDB. Tests stub it so they can run without a database.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from ragent.bootstrap.metrics import DocumentStatsCollector


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack,confluence")
    from ragent.bootstrap.metrics import _source_app_allowlist, _source_app_fallback

    _source_app_allowlist.cache_clear()
    _source_app_fallback.cache_clear()


def _sample_value(metric, labels: dict[str, str]) -> float | None:
    for fam in metric.collect():
        if fam.name != "ragent_documents_total":
            continue
        for s in fam.samples:
            if s.name == "ragent_documents_total" and s.labels == labels:
                return s.value
    return None


def test_collector_emits_one_sample_per_group() -> None:
    rows: Iterable = [
        ("READY", "slack", "text/plain", 42),
        ("PENDING", "slack", "text/plain", 3),
        ("FAILED", "confluence", "text/markdown", 1),
    ]
    collector = DocumentStatsCollector(fetch_rows=lambda: list(rows))

    assert (
        _sample_value(
            collector,
            {"status": "READY", "source_app": "slack", "mime_type": "text/plain"},
        )
        == 42
    )
    assert (
        _sample_value(
            collector,
            {"status": "PENDING", "source_app": "slack", "mime_type": "text/plain"},
        )
        == 3
    )
    assert (
        _sample_value(
            collector,
            {
                "status": "FAILED",
                "source_app": "confluence",
                "mime_type": "text/markdown",
            },
        )
        == 1
    )


def test_collector_normalizes_source_app() -> None:
    collector = DocumentStatsCollector(
        fetch_rows=lambda: [("READY", "some-tenant", "text/plain", 7)]
    )
    assert (
        _sample_value(
            collector,
            {"status": "READY", "source_app": "other", "mime_type": "text/plain"},
        )
        == 7
    )


def test_collector_handles_null_mime_type() -> None:
    collector = DocumentStatsCollector(fetch_rows=lambda: [("READY", "slack", None, 5)])
    assert (
        _sample_value(
            collector,
            {"status": "READY", "source_app": "slack", "mime_type": "text/plain"},
        )
        == 5
    )


def test_collector_swallows_fetch_errors_and_emits_nothing() -> None:
    def _boom() -> Iterable:
        raise RuntimeError("db down")

    collector = DocumentStatsCollector(fetch_rows=_boom)
    families = list(collector.collect())
    # Empty family is acceptable (no samples) — scrape stays 200.
    assert all(len(f.samples) == 0 for f in families)


def test_collector_aggregates_rows_that_normalize_to_same_labels() -> None:
    """Two unknown source_apps both collapse to 'other' → one summed sample.

    `prometheus_client.GaugeMetricFamily.add_metric` rejects duplicate label
    sets on render, so the collector must dedupe before emitting.
    """
    collector = DocumentStatsCollector(
        fetch_rows=lambda: [
            ("READY", "tenant-a", "text/plain", 3),
            ("READY", "tenant-b", "text/plain", 4),
            ("READY", "tenant-c", None, 5),  # mime → text/plain, source_app → other
        ]
    )
    sample = _sample_value(
        collector,
        {"status": "READY", "source_app": "other", "mime_type": "text/plain"},
    )
    assert sample == 12
    # And the rendered output must round-trip without error.
    list(collector.collect())
