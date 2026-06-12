"""Worker promote failure must still terminalize the document (issue #170).

``promote_to_ready_and_demote_siblings`` runs after the pipeline ``try`` block;
if it raises (e.g. deadlock retries exhausted), the task previously died with
the row stranded in PENDING forever — no broker retry, and the reconciler is
the only rescue. The worker must catch any promote failure and transition the
row PENDING → FAILED with a typed error code.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from ragent.errors.codes import TaskErrorCode
from ragent.repositories.document_repository import DocumentRow
from ragent.utility.state_machine import IllegalStateTransition
from tests.conftest import make_ingest_container


def _doc() -> DocumentRow:
    now = datetime.datetime.now(datetime.timezone.utc)
    return DocumentRow(
        document_id="DOC001",
        create_user="alice",
        source_id="S1",
        source_app="confluence",
        source_title="T",
        source_meta=None,
        object_key="confluence_S1_DOC001",
        status="UPLOADED",
        attempt=0,
        created_at=now,
        updated_at=now,
    )


def _deadlock_exc() -> OperationalError:
    cause = MagicMock()
    cause.args = (1213, "Deadlock found when trying to get lock")
    return OperationalError("UPDATE documents ...", {}, cause)


@pytest.mark.asyncio
async def test_promote_failure_marks_document_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promote raising → PENDING → FAILED with PIPELINE_UNEXPECTED_ERROR, no raise."""
    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "5")
    container = make_ingest_container(_doc())
    container.doc_repo.promote_to_ready_and_demote_siblings.side_effect = _deadlock_exc()

    from ragent.workers.ingest import ingest_pipeline_task

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")  # must not raise

    container.doc_repo.update_status.assert_awaited_once()
    kwargs = container.doc_repo.update_status.call_args.kwargs
    assert kwargs["from_status"] == "PENDING"
    assert kwargs["to_status"] == "FAILED"
    assert kwargs["error_code"] == TaskErrorCode.PIPELINE_UNEXPECTED_ERROR
    # Post-READY enrichment must not run for a doc that never reached READY.
    container.registry.fan_out.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_failure_tolerates_row_already_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a sibling demoted the row meanwhile, FAILED transition rowcount=0 is benign."""
    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "5")
    container = make_ingest_container(_doc())
    container.doc_repo.promote_to_ready_and_demote_siblings.side_effect = _deadlock_exc()
    container.doc_repo.update_status.side_effect = IllegalStateTransition(
        "update_status: PENDING → FAILED failed for DOC001"
    )

    from ragent.workers.ingest import ingest_pipeline_task

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")  # must not raise


@pytest.mark.asyncio
async def test_promote_failure_tolerates_update_status_lock_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_status itself raising (e.g. lock error) must not escape the task."""
    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "5")
    container = make_ingest_container(_doc())
    container.doc_repo.promote_to_ready_and_demote_siblings.side_effect = _deadlock_exc()
    container.doc_repo.update_status.side_effect = _deadlock_exc()

    from ragent.workers.ingest import ingest_pipeline_task

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")  # must not raise
