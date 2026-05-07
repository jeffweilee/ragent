"""ragent_pipeline_runs_total — outcome counter for ingest pipeline runs."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from ragent.bootstrap.metrics import record_pipeline_outcome


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack,confluence")
    from ragent.bootstrap.metrics import _source_app_allowlist, _source_app_fallback

    _source_app_allowlist.cache_clear()
    _source_app_fallback.cache_clear()


def _value(source_app: str, mime_type: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "ragent_pipeline_runs_total",
            {"source_app": source_app, "mime_type": mime_type, "outcome": outcome},
        )
        or 0.0
    )


def test_success_outcome_increments() -> None:
    before = _value("slack", "text/plain", "success")
    record_pipeline_outcome(source_app="slack", mime_type="text/plain", outcome="success")
    assert _value("slack", "text/plain", "success") == before + 1


def test_failed_outcome_increments_separately() -> None:
    before_ok = _value("slack", "text/plain", "success")
    before_fail = _value("slack", "text/plain", "failed")
    record_pipeline_outcome(source_app="slack", mime_type="text/plain", outcome="failed")
    assert _value("slack", "text/plain", "success") == before_ok
    assert _value("slack", "text/plain", "failed") == before_fail + 1


def test_unknown_source_app_collapses_to_fallback() -> None:
    before = _value("other", "text/plain", "success")
    record_pipeline_outcome(source_app="some-tenant", mime_type="text/plain", outcome="success")
    assert _value("other", "text/plain", "success") == before + 1


def test_none_mime_falls_back_to_text_plain() -> None:
    before = _value("slack", "text/plain", "success")
    record_pipeline_outcome(source_app="slack", mime_type=None, outcome="success")
    assert _value("slack", "text/plain", "success") == before + 1
