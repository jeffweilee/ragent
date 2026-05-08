"""T5.1 — Reconciler: stale PENDING rows re-dispatched to ingest.pipeline (B16, S2, S33)."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.repositories.document_repository import DocumentRow

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _dt(seconds_ago: int = 0) -> datetime.datetime:
    return _BASE - datetime.timedelta(seconds=seconds_ago)


def _make_doc(
    doc_id: str,
    status: str = "PENDING",
    attempt: int = 1,
    seconds_ago: int = 600,
) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_meta=None,
        object_key=f"confluence_S1_{doc_id}",
        status=status,
        attempt=attempt,
        created_at=_dt(1000),
        updated_at=_dt(seconds_ago),
    )


def _make_reconciler(repo: AsyncMock, broker: MagicMock):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker)


def _default_repo(pending_stale: list | None = None) -> AsyncMock:
    repo = AsyncMock()
    repo.list_pending_stale.return_value = pending_stale or []
    repo.list_pending_exceeded.return_value = []
    repo.list_deleting_stale.return_value = []
    repo.list_uploaded_stale.return_value = []
    repo.find_multi_ready_groups.return_value = []
    return repo


# ---------------------------------------------------------------------------
# Stale PENDING → re-kiq
# ---------------------------------------------------------------------------


def test_stale_pending_is_redispatched():
    """PENDING row older than threshold is re-enqueued to ingest.pipeline."""
    repo = _default_repo(pending_stale=[_make_doc("DOC001", attempt=1)])
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_called_once_with("ingest.pipeline", document_id="DOC001")


def test_multiple_stale_pending_all_redispatched():
    """All stale PENDING rows are re-enqueued."""
    stale_docs = [_make_doc(f"DOC{i:03d}", attempt=i) for i in range(1, 4)]
    repo = _default_repo(pending_stale=stale_docs)
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    dispatched_ids = {c.kwargs["document_id"] for c in broker.enqueue.call_args_list}
    assert dispatched_ids == {"DOC001", "DOC002", "DOC003"}


def test_fresh_pending_not_redispatched():
    """Empty stale list (fresh rows excluded by query) → no dispatch."""
    repo = _default_repo()
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Correct query parameters passed to the repo
# ---------------------------------------------------------------------------


def test_list_pending_stale_called_with_attempt_le(monkeypatch: pytest.MonkeyPatch):
    """list_pending_stale receives attempt_le = WORKER_MAX_ATTEMPTS (default 5)."""
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("RECONCILER_PENDING_STALE_SECONDS", "300")

    repo = _default_repo()
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    repo.list_pending_stale.assert_called_once()
    _, kwargs = repo.list_pending_stale.call_args
    assert (
        kwargs.get(
            "attempt_le",
            repo.list_pending_stale.call_args[0][1]
            if repo.list_pending_stale.call_args[0]
            else None,
        )
        == 5
    )


def test_list_pending_stale_custom_max_attempts(monkeypatch: pytest.MonkeyPatch):
    """WORKER_MAX_ATTEMPTS env var controls the attempt_le threshold."""
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "3")

    repo = _default_repo()
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    _, kwargs = repo.list_pending_stale.call_args
    attempt_le = kwargs.get("attempt_le") if kwargs else repo.list_pending_stale.call_args[0][1]
    assert attempt_le == 3


# ---------------------------------------------------------------------------
# Idempotency (S2)
# ---------------------------------------------------------------------------


def test_idempotent_second_run_dispatches_again():
    """Re-running the reconciler re-dispatches still-stale rows each cycle (S2)."""
    repo = _default_repo(pending_stale=[_make_doc("DOC001")])
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()
    rec.run()

    assert broker.enqueue.call_count == 2
