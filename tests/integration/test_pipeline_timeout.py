"""T3.2j — Pipeline timeout: overrun → FAILED status; MinIO not deleted (S34, B18)."""

import datetime
from unittest.mock import patch

import pytest

from ragent.repositories.document_repository import DocumentRow
from tests.conftest import make_ingest_container

pytestmark = pytest.mark.docker


def _make_doc() -> DocumentRow:
    return DocumentRow(
        document_id="DOC001",
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_workspace=None,
        object_key="confluence_S1_DOC001",
        status="UPLOADED",
        attempt=0,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
    )


async def test_pipeline_timeout_transitions_to_failed():
    """TimeoutError in pipeline body → update_status to FAILED (S34)."""
    from ragent.workers.ingest import ingest_pipeline_task

    container = make_ingest_container(
        _make_doc(), pipeline_side_effect=TimeoutError("pipeline timeout")
    )

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    to_statuses = [
        c.kwargs.get("to_status") or (c.args[2] if len(c.args) > 2 else None)
        for c in container.doc_repo.update_status.call_args_list
    ]
    assert "FAILED" in to_statuses


async def test_pipeline_timeout_does_not_delete_minio():
    """On timeout, MinIO object is NOT deleted (pipeline did not fully process file)."""
    from ragent.workers.ingest import ingest_pipeline_task

    container = make_ingest_container(
        _make_doc(), pipeline_side_effect=TimeoutError("pipeline timeout")
    )

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    container.minio_client.delete_object.assert_not_called()
