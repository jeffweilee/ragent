"""T5.13 — Reconciler heartbeat: tick counter + event=reconciler.tick log (R8, S30)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _default_repo() -> MagicMock:
    repo = MagicMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = []
    repo.list_deleting_stale.return_value = []
    repo.list_uploaded_stale.return_value = []
    repo.find_multi_ready_groups.return_value = []
    return repo


def _make_reconciler(repo: MagicMock, broker: MagicMock):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker)


# ---------------------------------------------------------------------------
# Prometheus counter
# ---------------------------------------------------------------------------


def test_run_increments_reconciler_tick_total():
    """Each run() call increments reconciler_tick_total by 1."""

    from ragent.bootstrap.telemetry import reconciler_tick_total

    before = reconciler_tick_total._value.get()

    repo = _default_repo()
    rec = _make_reconciler(repo, AsyncMock())
    rec.run()

    after = reconciler_tick_total._value.get()
    assert after == before + 1


def test_two_runs_increment_by_two():
    """Two run() calls increment the counter by 2."""
    from ragent.bootstrap.telemetry import reconciler_tick_total

    before = reconciler_tick_total._value.get()

    repo = _default_repo()
    rec = _make_reconciler(repo, AsyncMock())
    rec.run()
    rec.run()

    after = reconciler_tick_total._value.get()
    assert after == before + 2


# ---------------------------------------------------------------------------
# Structured log
# ---------------------------------------------------------------------------


def test_run_emits_reconciler_tick_log(caplog: pytest.LogCaptureFixture):
    """run() emits a structured-log line with event=reconciler.tick."""
    import structlog

    repo = _default_repo()
    rec = _make_reconciler(repo, AsyncMock())

    with structlog.testing.capture_logs() as logs:
        rec.run()

    assert any(e.get("event") == "reconciler.tick" for e in logs)
