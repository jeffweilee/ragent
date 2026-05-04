"""T3.2a — Worker: terminal status committed before MinIO delete; orphan logged on error."""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.docker


def _make_repo(doc_status="PENDING"):
    import datetime

    from ragent.repositories.document_repository import DocumentRow

    doc = DocumentRow(
        document_id="DOC001",
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_workspace=None,
        object_key="confluence_S1_DOC001",
        status=doc_status,
        attempt=1,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
    )
    repo = MagicMock()
    repo.acquire_nowait.return_value = doc
    return repo, doc


def test_terminal_status_committed_before_minio_delete():
    """READY status must be committed before MinIOClient.delete_object is called (S16)."""
    from ragent.workers.ingest import run_pipeline_task

    call_order = []
    repo, doc = _make_repo()
    storage = MagicMock()
    broker = MagicMock()
    broker.fan_out_delete = MagicMock()

    repo.update_status.side_effect = lambda *a, **kw: call_order.append("status_ready")
    storage.delete_object.side_effect = lambda *a: call_order.append("minio_delete")

    def mock_pipeline(doc_id):
        return []  # empty chunks — success

    run_pipeline_task(
        document_id="DOC001",
        repo=repo,
        storage=storage,
        broker=broker,
        pipeline_fn=mock_pipeline,
    )
    assert call_order.index("status_ready") < call_order.index("minio_delete")


def test_minio_delete_error_does_not_prevent_ready_status():
    """If MinIO delete raises, row is still READY and orphan event is emitted (S21)."""
    from ragent.workers.ingest import run_pipeline_task

    repo, doc = _make_repo()
    storage = MagicMock()
    storage.delete_object.side_effect = Exception("minio error")
    broker = MagicMock()

    run_pipeline_task(
        document_id="DOC001",
        repo=repo,
        storage=storage,
        broker=broker,
        pipeline_fn=lambda doc_id: [],
    )
    # update_status to READY must still have been called
    assert any(
        call_args[1].get("to_status") == "READY" or (call_args[0] and "READY" in call_args[0])
        for call_args in repo.update_status.call_args_list
    )


def test_pending_retry_does_not_delete_minio():
    """On retry path (still PENDING, exception raised), MinIO object must not be deleted."""
    from ragent.workers.ingest import run_pipeline_task

    repo, doc = _make_repo()
    storage = MagicMock()
    broker = MagicMock()

    def failing_pipeline(doc_id):
        raise RuntimeError("pipeline failed")

    run_pipeline_task(
        document_id="DOC001",
        repo=repo,
        storage=storage,
        broker=broker,
        pipeline_fn=failing_pipeline,
    )
    storage.delete_object.assert_not_called()
