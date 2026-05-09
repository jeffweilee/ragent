"""Aggregate ingest pipeline timeout — bounds total wall-clock per document."""

from __future__ import annotations

import datetime
import time
from unittest.mock import patch

import pytest

from ragent.repositories.document_repository import DocumentRow
from tests.conftest import make_ingest_container


def _doc() -> DocumentRow:
    now = datetime.datetime.now(datetime.UTC)
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


@pytest.mark.asyncio
async def test_aggregate_timeout_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipeline body exceeding INGEST_PIPELINE_TIMEOUT_SECONDS → FAILED."""
    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "0.1")

    container = make_ingest_container(_doc())
    # Simulate a pipeline that sleeps past the aggregate budget.
    container.ingest_pipeline.run.side_effect = lambda *a, **kw: (time.sleep(1.0), {})[1]

    from ragent.workers.ingest import ingest_pipeline_task

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    to_statuses = [
        c.kwargs.get("to_status") or (c.args[2] if len(c.args) > 2 else None)
        for c in container.doc_repo.update_status.call_args_list
    ]
    assert "FAILED" in to_statuses
    # MinIO inline object MUST NOT be deleted on timeout — caller may retry.
    container.minio_registry.delete_object.assert_not_called()


@pytest.mark.asyncio
async def test_aggregate_timeout_default_does_not_apply_when_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast pipeline below budget → READY, not FAILED."""
    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "5")

    container = make_ingest_container(_doc())
    container.ingest_pipeline.run.return_value = {"writer": {"documents_written": []}}

    from ragent.workers.ingest import ingest_pipeline_task

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await ingest_pipeline_task("DOC001")

    # B39: worker's READY transition routes through
    # promote_to_ready_and_demote_siblings (atomic with sibling demote).
    container.doc_repo.promote_to_ready_and_demote_siblings.assert_awaited_once()
    to_statuses = [
        c.kwargs.get("to_status") or (c.args[2] if len(c.args) > 2 else None)
        for c in container.doc_repo.update_status.call_args_list
    ]
    assert "FAILED" not in to_statuses


@pytest.mark.asyncio
async def test_legit_replacement_chars_in_source_do_not_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bytes containing legit U+FFFD must not trigger ingest.utf8_decode_replaced."""
    from structlog.testing import capture_logs

    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "5")
    container = make_ingest_container(_doc())
    # 'A' + U+FFFD encoded in valid UTF-8 (0xEF 0xBF 0xBD).
    legit = "A�".encode()
    container.minio_registry.head_object.return_value = (len(legit), "text/plain")
    container.minio_registry.get_object.return_value = legit

    from ragent.workers.ingest import ingest_pipeline_task

    with (
        capture_logs() as logs,
        patch("ragent.bootstrap.composition.get_container", return_value=container),
    ):
        await ingest_pipeline_task("DOC001")

    assert not any(rec.get("event") == "ingest.utf8_decode_replaced" for rec in logs)


@pytest.mark.asyncio
async def test_invalid_utf8_bytes_emit_replacement_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setenv("INGEST_PIPELINE_TIMEOUT_SECONDS", "5")
    container = make_ingest_container(_doc())
    bad = b"hello\xffworld"  # 0xff is never valid UTF-8
    container.minio_registry.head_object.return_value = (len(bad), "text/plain")
    container.minio_registry.get_object.return_value = bad

    from ragent.workers.ingest import ingest_pipeline_task

    with (
        capture_logs() as logs,
        patch("ragent.bootstrap.composition.get_container", return_value=container),
    ):
        await ingest_pipeline_task("DOC001")

    matches = [r for r in logs if r.get("event") == "ingest.utf8_decode_replaced"]
    assert matches and matches[0]["replacement_count"] == 1
