"""T3.2a — Worker: terminal status committed before MinIO delete; orphan logged on error."""

import datetime
from unittest.mock import patch

import pytest

from ragent.repositories.document_repository import DocumentRow
from tests.conftest import make_ingest_container

pytestmark = pytest.mark.docker


def _make_doc(doc_status: str = "UPLOADED") -> DocumentRow:
    return DocumentRow(
        document_id="DOC001",
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_meta=None,
        object_key="confluence_S1_DOC001",
        status=doc_status,
        attempt=1,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
    )


async def test_terminal_status_committed_before_minio_delete():
    """READY status must be committed before MinIO.delete_object is called (S16).

    B39: the READY transition is now `promote_to_ready_and_demote_siblings`,
    so the ordering check pivots on that call.
    """
    from ragent.workers.ingest import ingest_pipeline_task

    doc = _make_doc()
    container = make_ingest_container(doc)

    call_order: list[str] = []

    async def _record_promote(**kwargs):
        call_order.append("status_READY")
        return True

    container.doc_repo.promote_to_ready_and_demote_siblings.side_effect = _record_promote
    container.minio_registry.delete_object.side_effect = lambda *a: call_order.append(
        "minio_delete"
    )

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    assert "status_READY" in call_order
    assert "minio_delete" in call_order
    assert call_order.index("status_READY") < call_order.index("minio_delete")


async def test_minio_delete_error_does_not_prevent_ready_status():
    """If MinIO delete raises, row is still READY and orphan is tolerated (S21)."""
    from ragent.workers.ingest import ingest_pipeline_task

    doc = _make_doc()
    container = make_ingest_container(doc)
    container.minio_registry.delete_object.side_effect = Exception("minio error")

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    container.doc_repo.promote_to_ready_and_demote_siblings.assert_awaited_once()


async def test_pending_retry_does_not_delete_minio():
    """On pipeline failure, MinIO object must not be deleted."""
    from ragent.workers.ingest import ingest_pipeline_task

    doc = _make_doc()
    container = make_ingest_container(doc, pipeline_side_effect=RuntimeError("pipeline failed"))

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    container.minio_registry.delete_object.assert_not_called()


async def test_self_demote_skips_post_ready_fan_out():
    """B41: when promote loses arbitration (older worker, newer revision exists),
    the worker self-demotes to DELETING — post-READY enrichment (`fan_out`) must
    NOT run for a non-READY doc.
    """
    from ragent.workers.ingest import ingest_pipeline_task

    doc = _make_doc()
    container = make_ingest_container(doc)
    container.doc_repo.promote_to_ready_and_demote_siblings.return_value = False

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    container.registry.fan_out.assert_not_called()
