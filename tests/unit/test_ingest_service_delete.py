"""T2.9 — IngestService.delete: cascade order, idempotent re-delete (S12, S13, S14, P-E)."""

import datetime
from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.document_repository import DocumentRow, LockNotAvailable
from ragent.services.ingest_service import IngestService


def _dt():
    return datetime.datetime.now(datetime.UTC)


def _make_doc(**kwargs):
    base = dict(
        document_id="DOCID001",
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_workspace=None,
        object_key="confluence_S1_DOCID001",
        status="READY",
        attempt=1,
        created_at=_dt(),
        updated_at=_dt(),
    )
    base.update(kwargs)
    return DocumentRow(**base)


def _make_service(doc=None, lock_raises=False):
    repo = AsyncMock()
    doc = doc or _make_doc()
    if lock_raises:
        repo.acquire_nowait.side_effect = LockNotAvailable("DOCID001")
    else:
        repo.acquire_nowait.return_value = doc

    chunks = AsyncMock()
    storage = MagicMock()
    plugin_registry = MagicMock()

    svc = IngestService(repo=repo, chunks=chunks, storage=storage, broker=plugin_registry)
    return svc, repo, chunks, storage, plugin_registry


async def test_delete_ready_doc_calls_cascade_in_order():
    """DELETING status set before any external calls (spec §3.1)."""
    call_order = []
    svc, repo, chunks, storage, registry = _make_service()
    repo.update_status.side_effect = AsyncMock(
        side_effect=lambda *a, **kw: call_order.append("status_deleting")
    )
    registry.fan_out_delete = MagicMock(side_effect=lambda *a: call_order.append("fan_out_delete"))
    chunks.delete_by_document_id.side_effect = AsyncMock(
        side_effect=lambda *a: call_order.append("delete_chunks")
    )
    repo.delete.side_effect = AsyncMock(side_effect=lambda *a: call_order.append("delete_row"))
    storage.delete_object.side_effect = lambda *a: call_order.append("delete_minio")

    await svc.delete("DOCID001")

    assert call_order[0] == "status_deleting"
    assert call_order[-1] == "delete_row"


async def test_delete_idempotent_on_missing_doc():
    """Re-DELETE of already-deleted document returns without error (S14)."""
    repo = AsyncMock()
    repo.acquire_nowait.side_effect = LockNotAvailable("NONEXISTENT")
    svc = IngestService(repo=repo, chunks=AsyncMock(), storage=MagicMock(), broker=MagicMock())
    await svc.delete("NONEXISTENT")  # must not raise
    repo.delete.assert_not_called()


async def test_delete_uploaded_doc_deletes_minio_object():
    """UPLOADED status → MinIO staging object is deleted as part of cascade (S12)."""
    doc = _make_doc(status="UPLOADED")
    svc, repo, _chunks, storage, _ = _make_service(doc=doc)
    await svc.delete("DOCID001")
    storage.delete_object.assert_called_once_with("confluence_S1_DOCID001")


async def test_delete_pending_doc_deletes_minio_object():
    """PENDING status → MinIO staging object deleted (file still in staging)."""
    doc = _make_doc(status="PENDING")
    svc, repo, _chunks, storage, _ = _make_service(doc=doc)
    await svc.delete("DOCID001")
    storage.delete_object.assert_called_once()


async def test_delete_ready_doc_does_not_delete_minio():
    """READY status → MinIO already cleared at pipeline terminal; no delete call."""
    doc = _make_doc(status="READY")
    svc, repo, _chunks, storage, _ = _make_service(doc=doc)
    await svc.delete("DOCID001")
    storage.delete_object.assert_not_called()


async def test_delete_minio_failure_does_not_stop_cascade():
    """Fan_out_delete runs outside tx; storage error tolerated (P-E)."""
    doc = _make_doc(status="UPLOADED")
    svc, repo, _chunks, storage, _ = _make_service(doc=doc)
    storage.delete_object.side_effect = Exception("storage error")
    await svc.delete("DOCID001")  # must not raise
    repo.delete.assert_called_once()


async def test_delete_calls_fan_out_delete_outside_tx():
    """fan_out_delete is called with no DB tx open — only structural verification here."""
    svc, repo, _chunks, storage, registry = _make_service()
    registry.fan_out_delete = MagicMock()
    await svc.delete("DOCID001")
    registry.fan_out_delete.assert_called_once_with("DOCID001")


async def test_delete_calls_delete_chunks():
    svc, _repo, chunks, _, _ = _make_service()
    await svc.delete("DOCID001")
    chunks.delete_by_document_id.assert_called_once_with("DOCID001")
