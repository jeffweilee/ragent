"""T5.11 — Reconciler: FAILED transition commits status first, then fans out
plugin cleanup (R5, S27).

C6 dropped ``ChunkRepository``; the ES-side cleanup is now owned by the
vector plugin (registered with ``PluginRegistry`` and invoked via
``fan_out_delete``).
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.document_repository import DocumentRow

_BASE = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _dt(seconds_ago: int = 0) -> datetime.datetime:
    return _BASE - datetime.timedelta(seconds=seconds_ago)


def _make_exceeded_doc(doc_id: str, attempt: int = 6) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_meta=None,
        object_key=f"confluence_S1_{doc_id}",
        status="PENDING",
        attempt=attempt,
        created_at=_dt(1000),
        updated_at=_dt(600),
    )


def _default_repo(exceeded: list | None = None) -> AsyncMock:
    repo = AsyncMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = exceeded or []
    repo.list_deleting_stale.return_value = []
    repo.list_uploaded_stale.return_value = []
    repo.find_multi_ready_groups.return_value = []
    return repo


def _make_reconciler(
    repo: AsyncMock,
    broker: MagicMock,
    registry: MagicMock | None = None,
):
    from ragent.reconciler import Reconciler

    return Reconciler(repo=repo, broker=broker, registry=registry)


def test_failed_status_committed_before_fan_out_cleanup():
    """update_status(FAILED) runs before registry.fan_out_delete (Rule 21)."""
    repo = _default_repo(exceeded=[_make_exceeded_doc("DOC001")])
    broker = AsyncMock()
    registry = MagicMock()
    registry.fan_out_delete = AsyncMock(return_value=[])

    call_order: list[str] = []
    registry.fan_out_delete.side_effect = lambda doc_id: call_order.append("fan_out_delete")
    repo.update_status.side_effect = lambda *a, **kw: call_order.append("update_status")

    rec = _make_reconciler(repo, broker, registry=registry)
    rec.run()

    assert "update_status" in call_order
    assert call_order.index("update_status") < call_order.index("fan_out_delete")


def test_failed_cleanup_no_registry_still_marks_failed():
    """Without registry, FAILED transition still happens."""
    repo = _default_repo(exceeded=[_make_exceeded_doc("DOC001")])
    broker = AsyncMock()
    rec = _make_reconciler(repo, broker, registry=None)
    rec.run()
    repo.update_status.assert_called_once_with("DOC001", from_status="PENDING", to_status="FAILED")


def test_failed_cleanup_fan_out_receives_correct_doc_id():
    """fan_out_delete is called with the correct document_id."""
    repo = _default_repo(exceeded=[_make_exceeded_doc("DOC999")])
    broker = AsyncMock()
    registry = MagicMock()
    registry.fan_out_delete = AsyncMock(return_value=[])

    rec = _make_reconciler(repo, broker, registry=registry)
    rec.run()

    registry.fan_out_delete.assert_called_once_with("DOC999")
