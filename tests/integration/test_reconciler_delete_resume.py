"""T5.5 — Reconciler: stale DELETING rows resume cascade idempotently (S13, B28).

C6 dropped ``ChunkRepository``; the cascade is now: fan_out_delete (plugins
clean ES) → repo.delete (the v1 ``chunks.delete_by_document_id`` step is
gone — chunks live exclusively in ES, removed by the vector plugin).
"""

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
        source_meta=None,
        object_key=f"confluence_S1_{doc_id}",
        status=status,
        attempt=1,
        created_at=_dt(1000),
        updated_at=_dt(seconds_ago),
    )


def _default_repo(deleting: list | None = None) -> AsyncMock:
    repo = AsyncMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = []
    repo.list_deleting_stale.return_value = deleting or []
    repo.find_multi_ready_groups.return_value = []
    repo.list_uploaded_stale.return_value = []
    return repo


def _make_reconciler(
    repo: AsyncMock,
    broker: MagicMock,
    registry: MagicMock | None = None,
):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker, registry=registry)


def test_stale_deleting_resumes_cascade():
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    rec = _make_reconciler(repo, broker)
    rec.run()
    repo.delete.assert_called_once_with("DOC001")


def test_stale_deleting_no_redispatch():
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    rec = _make_reconciler(repo, broker)
    rec.run()
    broker.enqueue.assert_not_called()


def test_multiple_stale_deleting_all_resumed():
    docs = [_make_doc(f"DOC{i:03d}") for i in range(1, 4)]
    repo = _default_repo(deleting=docs)
    broker = AsyncMock()
    rec = _make_reconciler(repo, broker)
    rec.run()
    assert repo.delete.call_count == 3


def test_stale_deleting_cascade_is_idempotent():
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    rec = _make_reconciler(repo, broker)
    rec.run()
    rec.run()
    assert repo.delete.call_count == 2


def test_stale_deleting_calls_fan_out_delete_when_registry_present():
    repo = _default_repo(deleting=[_make_doc("DOC001")])
    broker = AsyncMock()
    registry = MagicMock()
    registry.fan_out_delete = AsyncMock(return_value=[])
    rec = _make_reconciler(repo, broker, registry=registry)
    rec.run()
    registry.fan_out_delete.assert_called_once_with("DOC001")


def test_list_deleting_stale_called_with_threshold(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECONCILER_DELETING_STALE_SECONDS", "300")
    repo = _default_repo()
    broker = AsyncMock()
    rec = _make_reconciler(repo, broker)
    rec.run()
    repo.list_deleting_stale.assert_called_once()
