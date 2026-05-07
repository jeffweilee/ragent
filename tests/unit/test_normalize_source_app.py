"""Cardinality cap for the `source_app` metric label.

Allow-list comes from `RAGENT_METRICS_SOURCE_APP_ALLOWLIST` (comma-separated).
Anything not in the allow-list collapses to `RAGENT_METRICS_SOURCE_APP_FALLBACK`
(default `"other"`) so the metric label set stays bounded.
"""

from __future__ import annotations

import pytest

from ragent.bootstrap.metrics import (
    _source_app_allowlist,
    _source_app_fallback,
    normalize_source_app,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    _source_app_allowlist.cache_clear()
    _source_app_fallback.cache_clear()


def test_value_in_allowlist_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack,confluence")
    assert normalize_source_app("slack") == "slack"
    assert normalize_source_app("confluence") == "confluence"


def test_value_not_in_allowlist_collapses_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack")
    assert normalize_source_app("notion") == "other"
    assert normalize_source_app("") == "other"
    assert normalize_source_app(None) == "other"


def test_empty_allowlist_collapses_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", raising=False)
    assert normalize_source_app("slack") == "other"


def test_whitespace_and_empty_entries_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "  slack ,, confluence  ,")
    assert normalize_source_app("slack") == "slack"
    assert normalize_source_app("confluence") == "confluence"


def test_custom_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack")
    monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_FALLBACK", "unknown")
    assert normalize_source_app("notion") == "unknown"
