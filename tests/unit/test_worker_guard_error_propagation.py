"""Worker preserves guard-defined `error_code` when an exception carrying
`error_code` propagates from the pipeline.

Without this contract, ArchiveBombError / PdfTooManyPagesError (which subclass
plain Exception, not IngestStepError) collapse to PIPELINE_UNEXPECTED_ERROR on
documents.error_code — operators investigating "why did this PPTX fail" see
only the generic code instead of INGEST_ARCHIVE_UNSAFE / INGEST_PDF_TOO_MANY_PAGES.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragent.errors.codes import HttpErrorCode
from ragent.repositories.document_repository import DocumentRow
from ragent.schemas.ingest import IngestMime
from ragent.security.archive_guard import ArchiveBombError, ArchiveBombReason, PdfTooManyPagesError


def _doc(mime_type: str) -> MagicMock:
    doc = MagicMock(spec=DocumentRow)
    doc.document_id = "DOC-PROP-1"
    doc.minio_site = "__default__"
    doc.object_key = "in.bin"
    doc.source_id = "S1"
    doc.source_app = "upload-cli"
    doc.source_url = None
    doc.source_title = "x"
    doc.source_meta = None
    doc.ingest_type = "inline"
    doc.attempt = 0
    doc.mime_type = mime_type
    return doc


def _container_that_raises(doc: MagicMock, exc: Exception) -> MagicMock:
    container = MagicMock()
    container.doc_repo = AsyncMock()
    container.doc_repo.claim_for_processing.return_value = doc
    container.doc_repo.update_status = AsyncMock()
    container.minio_registry = MagicMock()
    container.minio_registry.head_object.return_value = (32, "application/octet-stream")
    container.minio_registry.get_object.return_value = b"PK\x03\x04" + b"\x00" * 28
    container.ingest_pipeline.run.side_effect = exc
    container.registry = MagicMock()
    container.registry.fan_out = AsyncMock()
    container.unprotect_client = None
    container.embedding_registry.refresh = AsyncMock()
    return container


@pytest.mark.asyncio
async def test_archive_bomb_error_preserves_error_code_on_failed_row() -> None:
    doc = _doc(mime_type=IngestMime.PPTX)
    exc = ArchiveBombError(ArchiveBombReason.MEMBERS, "9001 > 5000")
    container = _container_that_raises(doc, exc)

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-PROP-1")

    call_kwargs = container.doc_repo.update_status.call_args.kwargs
    assert call_kwargs["to_status"] == "FAILED"
    assert call_kwargs["error_code"] == HttpErrorCode.INGEST_ARCHIVE_UNSAFE


@pytest.mark.asyncio
async def test_pdf_too_many_pages_error_preserves_error_code_on_failed_row() -> None:
    doc = _doc(mime_type=IngestMime.PDF)
    exc = PdfTooManyPagesError(page_count=10_000, cap=2000)
    container = _container_that_raises(doc, exc)

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-PROP-1")

    call_kwargs = container.doc_repo.update_status.call_args.kwargs
    assert call_kwargs["to_status"] == "FAILED"
    assert call_kwargs["error_code"] == HttpErrorCode.INGEST_PDF_TOO_MANY_PAGES
