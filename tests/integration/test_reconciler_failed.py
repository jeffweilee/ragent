"""T5.3 — Reconciler: PENDING rows exceeding max attempts → FAILED + structured log (S3, B28)."""

from __future__ import annotations

import datetime
import logging
from unittest.mock import MagicMock

import pytest

from ragent.repositories.document_repository import DocumentRow

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _dt(seconds_ago: int = 0) -> datetime.datetime:
    return _BASE - datetime.timedelta(seconds=seconds_ago)


def _make_doc(doc_id: str, attempt: int = 6, seconds_ago: int = 600) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_workspace=None,
        object_key=f"confluence_S1_{doc_id}",
        status="PENDING",
        attempt=attempt,
        created_at=_dt(1000),
        updated_at=_dt(seconds_ago),
    )


def _make_reconciler(repo: MagicMock, broker: MagicMock):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker)


def _default_repo(exceeded: list | None = None) -> MagicMock:
    repo = MagicMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = exceeded or []
    repo.find_multi_ready_groups.return_value = []
    repo.list_uploaded_stale.return_value = []
    return repo


# ---------------------------------------------------------------------------
# Exceeded → FAILED transition
# ---------------------------------------------------------------------------


def test_exceeded_attempt_transitions_to_failed():
    """PENDING doc with attempt > WORKER_MAX_ATTEMPTS is transitioned to FAILED."""
    repo = _default_repo(exceeded=[_make_doc("DOC001", attempt=6)])
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    repo.update_status.assert_called_once_with("DOC001", from_status="PENDING", to_status="FAILED")


def test_exceeded_attempt_does_not_redispatch():
    """Doc exceeding max attempts must not be re-enqueued to ingest.pipeline."""
    repo = _default_repo(exceeded=[_make_doc("DOC001", attempt=6)])
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_not_called()


def test_multiple_exceeded_all_failed():
    """All PENDING docs exceeding max attempts are transitioned to FAILED."""
    repo = _default_repo(exceeded=[_make_doc(f"DOC{i:03d}", attempt=6) for i in range(1, 4)])
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    assert repo.update_status.call_count == 3
    failed_ids = {c.args[0] for c in repo.update_status.call_args_list}
    assert failed_ids == {"DOC001", "DOC002", "DOC003"}


def test_list_pending_exceeded_called_with_max_attempts(monkeypatch: pytest.MonkeyPatch):
    """list_pending_exceeded receives attempt_gt = WORKER_MAX_ATTEMPTS (default 5)."""
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "5")

    repo = _default_repo()
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    repo.list_pending_exceeded.assert_called_once()
    _, kwargs = repo.list_pending_exceeded.call_args
    attempt_gt = kwargs.get("attempt_gt") if kwargs else repo.list_pending_exceeded.call_args[0][0]
    assert attempt_gt == 5


def test_exceeded_emits_structured_log(caplog: pytest.LogCaptureFixture):
    """Failing a doc emits event=ingest.failed in the log."""
    repo = _default_repo(exceeded=[_make_doc("DOC001", attempt=6)])
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    with caplog.at_level(logging.INFO, logger="ragent.reconciler"):
        rec.run()

    assert any("ingest.failed" in r.message for r in caplog.records)
