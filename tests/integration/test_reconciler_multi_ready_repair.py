"""T5.9 — Reconciler: multi-READY same source → re-enqueue ingest.supersede (R3, S26)."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

from ragent.repositories.document_repository import DocumentRow

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _dt(seconds_ago: int = 0) -> datetime.datetime:
    return _BASE - datetime.timedelta(seconds=seconds_ago)


def _make_doc(
    doc_id: str,
    source_id: str = "S1",
    source_app: str = "confluence",
    created_offset: int = 0,
) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id=source_id,
        source_app=source_app,
        source_title="T",
        source_workspace=None,
        object_key=f"{source_app}_{source_id}_{doc_id}",
        status="READY",
        attempt=1,
        created_at=_BASE + datetime.timedelta(seconds=created_offset),
        updated_at=_BASE,
    )


def _default_repo(groups: list | None = None, ready_docs: list | None = None) -> MagicMock:
    repo = MagicMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = []
    repo.list_deleting_stale.return_value = []
    repo.list_uploaded_stale.return_value = []
    repo.find_multi_ready_groups.return_value = groups or []
    repo.list_ready_by_source.return_value = ready_docs or []
    return repo


def _make_reconciler(repo: MagicMock, broker: MagicMock):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker)


# ---------------------------------------------------------------------------
# Multi-READY → enqueue supersede
# ---------------------------------------------------------------------------


def test_multi_ready_enqueues_supersede():
    """Two READY docs for same source → reconciler enqueues ingest.supersede."""
    older = _make_doc("DOC001", created_offset=0)
    newer = _make_doc("DOC002", created_offset=10)
    repo = _default_repo(groups=[("S1", "confluence")], ready_docs=[older, newer])
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_called_once_with(
        "ingest.supersede",
        survivor_id="DOC002",
        source_id="S1",
        source_app="confluence",
    )


def test_no_multi_ready_no_supersede():
    """No multi-READY groups → no supersede enqueued."""
    repo = _default_repo(groups=[])
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    broker.enqueue.assert_not_called()


def test_multiple_groups_each_get_supersede():
    """Each conflicting group gets a separate supersede enqueue."""
    older_a = _make_doc("DOC001", source_id="S1", source_app="confluence", created_offset=0)
    newer_a = _make_doc("DOC002", source_id="S1", source_app="confluence", created_offset=10)
    older_b = _make_doc("DOC003", source_id="S2", source_app="slack", created_offset=0)
    newer_b = _make_doc("DOC004", source_id="S2", source_app="slack", created_offset=5)

    repo = _default_repo(
        groups=[("S1", "confluence"), ("S2", "slack")],
    )
    repo.list_ready_by_source.side_effect = [
        [older_a, newer_a],
        [older_b, newer_b],
    ]
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    assert broker.enqueue.call_count == 2
    calls = {c.kwargs["survivor_id"] for c in broker.enqueue.call_args_list}
    assert calls == {"DOC002", "DOC004"}


def test_survivor_is_newest_created_at():
    """Survivor is the doc with the maximum created_at in the group."""
    oldest = _make_doc("DOC001", created_offset=0)
    middle = _make_doc("DOC002", created_offset=5)
    newest = _make_doc("DOC003", created_offset=20)
    # list_ready_by_source returns ordered by created_at ASC
    repo = _default_repo(
        groups=[("S1", "confluence")],
        ready_docs=[oldest, middle, newest],
    )
    broker = MagicMock()

    rec = _make_reconciler(repo, broker)
    rec.run()

    _, kwargs = broker.enqueue.call_args
    assert kwargs["survivor_id"] == "DOC003"
