"""T3.2j — Pipeline timeout: overrun → FAILED with error_code=PIPELINE_TIMEOUT (S34, B18)."""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.docker


def _make_repo():
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
        status="PENDING",
        attempt=1,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
    )
    repo = MagicMock()
    repo.acquire_nowait.return_value = doc
    return repo, doc


def test_pipeline_timeout_transitions_to_failed():
    """Timeout in pipeline body → update_status to FAILED (S34)."""
    from ragent.workers.ingest import run_pipeline_task

    repo, doc = _make_repo()
    storage = MagicMock()
    broker = MagicMock()

    def slow_pipeline(doc_id):
        raise TimeoutError("pipeline timeout")

    run_pipeline_task(
        document_id="DOC001",
        repo=repo,
        storage=storage,
        broker=broker,
        pipeline_fn=slow_pipeline,
    )

    # Must have transitioned to FAILED
    failed_calls = [
        c
        for c in repo.update_status.call_args_list
        if c[1].get("to_status") == "FAILED" or (c[0] and "FAILED" in c[0])
    ]
    assert len(failed_calls) >= 1, "Expected FAILED status transition on timeout"


def test_pipeline_timeout_does_not_delete_minio():
    """On timeout, MinIO object is NOT deleted (pipeline did not fully process file)."""
    from ragent.workers.ingest import run_pipeline_task

    repo, doc = _make_repo()
    storage = MagicMock()
    broker = MagicMock()

    run_pipeline_task(
        document_id="DOC001",
        repo=repo,
        storage=storage,
        broker=broker,
        pipeline_fn=lambda doc_id: (_ for _ in ()).throw(TimeoutError("timeout")),
    )

    storage.delete_object.assert_not_called()
