"""T5.7 — Reconciler: stale UPLOADED rows re-dispatched to ingest.pipeline (R1, S24)."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.repositories.document_repository import DocumentRow

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _dt(seconds_ago: int = 0) -> datetime.datetime:
    return _BASE - datetime.timedelta(seconds=seconds_ago)


def _make_doc(doc_id: str, seconds_ago: int = 600) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_workspace=None,
        object_key=f"confluence_S1_{doc_id}",
        status="UPLOADED",
        attempt=0,
        created_at=_dt(1000),
        updated_at=_dt(seconds_ago),
    )


def _default_repo(uploaded: list | None = None) -> AsyncMock:
    repo = AsyncMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = []
    repo.list_deleting_stale.return_value = []
    repo.list_uploaded_stale.return_value = uploaded or []
    repo.find_multi_ready_groups.return_value = []
    return repo


def _make_reconciler(repo: AsyncMock, broker: MagicMock):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker)


# ---------------------------------------------------------------------------
# Stale UPLOADED → re-kiq ingest.pipeline
# ---------------------------------------------------------------------------


def test_stale_uploaded_is_redispatched():
    """UPLOADED row older than threshold is re-enqueued to ingest.pipeline."""
    repo = _default_repo(uploaded=[_make_doc("DOC001")])
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_called_once_with("ingest.pipeline", document_id="DOC001")


def test_multiple_stale_uploaded_all_redispatched():
    """All stale UPLOADED rows are re-enqueued."""
    repo = _default_repo(uploaded=[_make_doc(f"DOC{i:03d}") for i in range(1, 4)])
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    dispatched = {c.kwargs["document_id"] for c in broker.enqueue.call_args_list}
    assert dispatched == {"DOC001", "DOC002", "DOC003"}


def test_fresh_uploaded_not_redispatched():
    """Empty stale-uploaded list → no dispatch."""
    repo = _default_repo(uploaded=[])
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_not_called()


def test_list_uploaded_stale_called_with_threshold(monkeypatch: pytest.MonkeyPatch):
    """list_uploaded_stale receives updated_before based on RECONCILER_UPLOADED_STALE_SECONDS."""
    monkeypatch.setenv("RECONCILER_UPLOADED_STALE_SECONDS", "300")

    repo = _default_repo()
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    repo.list_uploaded_stale.assert_called_once()
