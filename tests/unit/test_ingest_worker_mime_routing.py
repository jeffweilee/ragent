"""Worker MIME routing: doc.mime_type (DB) takes precedence over MinIO content-type.

For file ingests the caller's MinIO may set content-type to a generic value
(application/octet-stream, None, etc.).  The declared mime_type on the DB row
is the authoritative routing key.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ragent.repositories.document_repository import DocumentRow
from ragent.schemas.ingest import IngestMime


def _doc(mime_type: str | None = None, minio_site: str | None = None) -> MagicMock:
    doc = MagicMock(spec=DocumentRow)
    doc.document_id = "DOC-MIME-1"
    doc.minio_site = minio_site or "__default__"
    doc.object_key = "deck.pptx"
    doc.source_id = "S1"
    doc.source_app = "upload-cli"
    doc.source_url = None
    doc.source_title = "Deck"
    doc.source_meta = None
    doc.ingest_type = "file" if minio_site else "inline"
    doc.attempt = 0
    doc.mime_type = mime_type
    return doc


def _container(doc: MagicMock, *, minio_content_type: str | None) -> MagicMock:
    container = MagicMock()
    container.doc_repo = AsyncMock()
    container.doc_repo.claim_for_processing.return_value = doc
    container.doc_repo.update_status = AsyncMock()
    container.doc_repo.promote_to_ready_and_demote_siblings = AsyncMock(return_value=True)
    container.minio_registry = MagicMock()
    # MinIO returns a generic content-type — but doc.mime_type should override it.
    container.minio_registry.head_object.return_value = (32, minio_content_type)
    # Minimal valid-ish bytes (worker just passes them through; pipeline is mocked).
    container.minio_registry.get_object.return_value = b"PK\x03\x04" + b"\x00" * 28
    container.ingest_pipeline.run.return_value = {"writer": {"documents_written": 1}}
    container.registry = MagicMock()
    container.registry.fan_out = AsyncMock()
    container.unprotect_client = None
    return container


@pytest.mark.asyncio
async def test_doc_mime_type_overrides_minio_content_type_for_binary_routing():
    """doc.mime_type=PPTX + MinIO content-type=octet-stream → binary path used."""
    doc = _doc(mime_type=IngestMime.PPTX, minio_site="corp")
    container = _container(doc, minio_content_type="application/octet-stream")

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-MIME-1")

    call_kwargs = container.ingest_pipeline.run.call_args[0][0]
    loader_kwargs = call_kwargs["loader"]
    # Binary path: content_bytes must be present; mime_type must be the declared PPTX MIME.
    assert "content_bytes" in loader_kwargs, "expected binary path (content_bytes)"
    assert loader_kwargs["mime_type"] == IngestMime.PPTX


@pytest.mark.asyncio
async def test_doc_mime_type_none_falls_back_to_minio_content_type():
    """Legacy doc (mime_type=None) falls back to MinIO content-type."""
    doc = _doc(mime_type=None)
    container = _container(doc, minio_content_type="text/plain")

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-MIME-1")

    call_kwargs = container.ingest_pipeline.run.call_args[0][0]
    loader_kwargs = call_kwargs["loader"]
    assert "content_bytes" not in loader_kwargs, "expected text path"
    assert loader_kwargs["mime_type"] == "text/plain"


@pytest.mark.asyncio
async def test_minio_content_type_uppercase_lowercased_for_routing():
    """MinIO metadata may return uppercase content-type; worker lowercases for routing."""
    doc = _doc(mime_type=None)
    container = _container(doc, minio_content_type="TEXT/PLAIN")

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-MIME-1")

    call_kwargs = container.ingest_pipeline.run.call_args[0][0]
    loader_kwargs = call_kwargs["loader"]
    assert loader_kwargs["mime_type"] == "text/plain"


@pytest.mark.asyncio
async def test_worker_binds_doc_mime_type_to_structlog_context():
    """Worker must bind doc.mime_type into the structlog context.

    Every ``ingest.step.*`` log inherits the context, so step logs for PPTX
    uploads must show the full PPTX MIME string rather than None.
    """
    from ragent.workers import ingest as worker_mod

    doc = _doc(mime_type=IngestMime.PPTX, minio_site="corp")
    container = _container(doc, minio_content_type="application/octet-stream")

    with (
        patch("ragent.bootstrap.composition.get_container", return_value=container),
        patch("ragent.workers.ingest.bind_ingest_context") as mock_bind,
    ):
        await worker_mod.ingest_pipeline_task("DOC-MIME-1")

    mock_bind.assert_called_once_with(document_id="DOC-MIME-1", mime_type=IngestMime.PPTX)
