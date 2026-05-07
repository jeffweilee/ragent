"""worker_pipeline_duration_seconds — observed at terminal pipeline transitions.

Re-labelled with source_app and mime_type (cap'd via the allow-list) so the
PromQL p95 panel can split by tenant/mime:
  histogram_quantile(0.95,
    sum by (le, source_app)
      (rate(worker_pipeline_duration_seconds_bucket[5m])))
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from ragent.bootstrap.metrics import observe_pipeline_duration


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack")
    from ragent.bootstrap.metrics import _source_app_allowlist, _source_app_fallback

    _source_app_allowlist.cache_clear()
    _source_app_fallback.cache_clear()


def _count(source_app: str, mime_type: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "worker_pipeline_duration_seconds_count",
            {"source_app": source_app, "mime_type": mime_type},
        )
        or 0.0
    )


def _sum(source_app: str, mime_type: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "worker_pipeline_duration_seconds_sum",
            {"source_app": source_app, "mime_type": mime_type},
        )
        or 0.0
    )


def test_observation_increments_count_and_sum() -> None:
    before_n = _count("slack", "text/plain")
    before_s = _sum("slack", "text/plain")
    observe_pipeline_duration(source_app="slack", mime_type="text/plain", seconds=12.5)
    assert _count("slack", "text/plain") == before_n + 1
    assert _sum("slack", "text/plain") == pytest.approx(before_s + 12.5)


def test_unknown_source_app_lands_in_fallback_bucket() -> None:
    before = _count("other", "text/plain")
    observe_pipeline_duration(source_app="some-tenant", mime_type="text/plain", seconds=1.0)
    assert _count("other", "text/plain") == before + 1


def test_none_mime_falls_back_to_text_plain() -> None:
    before = _count("slack", "text/plain")
    observe_pipeline_duration(source_app="slack", mime_type=None, seconds=1.0)
    assert _count("slack", "text/plain") == before + 1
