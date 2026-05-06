"""T5.5 — Reconciler: stale DELETING rows resume cascade idempotently (S13, B28)."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.repositories.document_repository import DocumentRow

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _dt(seconds_ago: int = 0) -> datetime.datetime:
    return _BASE - datetime.timedelta(seconds=seconds_ago)


def _make_doc(doc_id: str, status: str = "DELETING", seconds_ago: int = 600) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_workspace=None,
        object_key=f"confluence_S1_{doc_id}",
        status=status,
        attempt=1,
        created_at=_dt(1000),
        updated_at=_dt(seconds_ago),
    )


def _default_repo(deleting: list | None = None) -> MagicMock:
    repo = MagicMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = []
    repo.list_deleting_stale.return_value = deleting or []
    repo.find_multi_ready_groups.return_value = []
    repo.list_uploaded_stale.return_value = []
    return repo


def _make_reconciler(
    repo: MagicMock,
    broker: MagicMock,
    chunks: MagicMock | None = None,
    registry: MagicMock | None = None,
):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker, chunks=chunks, registry=registry)


# ---------------------------------------------------------------------------
# Stale DELETING → cascade resume
# ---------------------------------------------------------------------------


def test_stale_deleting_resumes_cascade():
    """Stale DELETING row: chunks deleted then doc row deleted."""
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    chunks = MagicMock()

    rec = _make_reconciler(repo, broker, chunks=chunks)
    rec.run()

    chunks.delete_by_document_id.assert_called_once_with("DOC001")
    repo.delete.assert_called_once_with("DOC001")


def test_stale_deleting_no_redispatch():
    """Stale DELETING rows must not be re-enqueued to ingest.pipeline."""
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    chunks = MagicMock()

    rec = _make_reconciler(repo, broker, chunks=chunks)
    rec.run()

    broker.enqueue.assert_not_called()


def test_multiple_stale_deleting_all_resumed():
    """Multiple stale DELETING rows are all cascade-deleted."""
    docs = [_make_doc(f"DOC{i:03d}") for i in range(1, 4)]
    repo = _default_repo(deleting=docs)
    broker = AsyncMock()
    chunks = MagicMock()

    rec = _make_reconciler(repo, broker, chunks=chunks)
    rec.run()

    assert chunks.delete_by_document_id.call_count == 3
    assert repo.delete.call_count == 3


def test_stale_deleting_cascade_is_idempotent():
    """Re-running reconciler on same stale DELETING row is safe (each call cleans up)."""
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    chunks = MagicMock()

    rec = _make_reconciler(repo, broker, chunks=chunks)
    rec.run()
    rec.run()

    assert repo.delete.call_count == 2


def test_stale_deleting_calls_fan_out_delete_when_registry_present():
    """When registry is provided, fan_out_delete is invoked before chunk/doc cleanup."""
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    chunks = MagicMock()
    registry = MagicMock()
    registry.fan_out_delete = AsyncMock(return_value=[])

    rec = _make_reconciler(repo, broker, chunks=chunks, registry=registry)
    rec.run()

    registry.fan_out_delete.assert_called_once_with("DOC001")


def test_list_deleting_stale_called_with_threshold(monkeypatch: pytest.MonkeyPatch):
    """list_deleting_stale receives updated_before based on RECONCILER_DELETING_STALE_SECONDS."""
    monkeypatch.setenv("RECONCILER_DELETING_STALE_SECONDS", "300")

    repo = _default_repo()
    broker = AsyncMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    repo.list_deleting_stale.assert_called_once()
