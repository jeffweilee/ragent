"""T-UP.2 — Worker unprotect-gate: bytes substitution when client is set."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from ragent.repositories.document_repository import DocumentRow
from tests.conftest import make_ingest_container


def _doc(mime_type: str | None = None, ingest_type: str = "file") -> DocumentRow:
    now = datetime.datetime.now(datetime.timezone.utc)
    return DocumentRow(
        document_id="DOC-UP-1",
        create_user="user-42",
        source_id="S1",
        source_app="test-app",
        source_title="Test Doc",
        source_meta=None,
        object_key="upload_src_DOC-UP-1",
        status="UPLOADED",
        attempt=0,
        created_at=now,
        updated_at=now,
        mime_type=mime_type,
        ingest_type=ingest_type,
    )


@pytest.mark.asyncio
async def test_unprotect_client_called_when_enabled():
    """When container.unprotect_client is set, worker calls it with MinIO bytes + user_id."""
    unprotect_mock = MagicMock()
    unprotect_mock.unprotect.return_value = b"unprotected-bytes"
    container = make_ingest_container(
        _doc(),
        unprotect_client=unprotect_mock,
        minio_bytes=b"original-bytes",
    )

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    unprotect_mock.unprotect.assert_called_once()
    call_kwargs = unprotect_mock.unprotect.call_args[1]
    assert call_kwargs["file_bytes"] == b"original-bytes"
    assert call_kwargs["user_id"] == "user-42"


@pytest.mark.asyncio
async def test_pipeline_receives_unprotected_bytes():
    """When unprotect is enabled, the pipeline gets the bytes returned by the client."""
    unprotect_mock = MagicMock()
    unprotect_mock.unprotect.return_value = b"clean-decrypted"
    container = make_ingest_container(
        _doc(),
        unprotect_client=unprotect_mock,
        minio_bytes=b"original-bytes",
    )

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    loader_kwargs = container.ingest_pipeline.run.call_args[0][0]["loader"]
    assert loader_kwargs["content"] == "clean-decrypted"


@pytest.mark.asyncio
async def test_pipeline_receives_original_bytes_when_unprotect_disabled():
    """When container.unprotect_client is None, original MinIO bytes pass through."""
    container = make_ingest_container(_doc(), minio_bytes=b"original-bytes")

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    loader_kwargs = container.ingest_pipeline.run.call_args[0][0]["loader"]
    assert loader_kwargs["content"] == "original-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime_type", "expected_ext"),
    [
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
        ("application/pdf", "pdf"),
        ("text/plain", "txt"),
        ("text/markdown", "md"),
        ("text/html", "html"),
    ],
)
async def test_unprotect_filename_carries_mime_extension(mime_type: str, expected_ext: str):
    """Worker appends mime-implied extension to object_key for unprotect fileInput."""
    unprotect_mock = MagicMock()
    unprotect_mock.unprotect.return_value = b"clean"
    doc = _doc(mime_type=mime_type)
    container = make_ingest_container(doc, unprotect_client=unprotect_mock)

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    call_kwargs = unprotect_mock.unprotect.call_args[1]
    assert call_kwargs["filename"] == f"{doc.object_key}.{expected_ext}"


@pytest.mark.asyncio
async def test_unprotect_filename_skips_duplicate_extension():
    """object_key already ending with the mime extension is passed through unchanged."""
    unprotect_mock = MagicMock()
    unprotect_mock.unprotect.return_value = b"clean"
    now = datetime.datetime.now(datetime.timezone.utc)
    doc = DocumentRow(
        document_id="DOC-UP-1",
        create_user="user-42",
        source_id="S1",
        source_app="test-app",
        source_title="Test Doc",
        source_meta=None,
        object_key="report.pptx",
        status="UPLOADED",
        attempt=0,
        created_at=now,
        updated_at=now,
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ingest_type="file",
    )
    container = make_ingest_container(doc, unprotect_client=unprotect_mock)

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    call_kwargs = unprotect_mock.unprotect.call_args[1]
    assert call_kwargs["filename"] == "report.pptx"


@pytest.mark.asyncio
async def test_unprotect_skipped_for_inline_ingest_type():
    """inline ingest_type bypasses unprotect even when the client is configured."""
    unprotect_mock = MagicMock()
    unprotect_mock.unprotect.return_value = b"should-not-be-used"
    container = make_ingest_container(
        _doc(ingest_type="inline"),
        unprotect_client=unprotect_mock,
        minio_bytes=b"original-inline",
    )

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    unprotect_mock.unprotect.assert_not_called()
    loader_kwargs = container.ingest_pipeline.run.call_args[0][0]["loader"]
    assert loader_kwargs["content"] == "original-inline"


@pytest.mark.asyncio
async def test_unprotect_failure_falls_back_to_original_bytes():
    """When unprotect raises, the pipeline continues with the original MinIO bytes."""
    unprotect_mock = MagicMock()
    unprotect_mock.unprotect.side_effect = RuntimeError("unprotect service unavailable")
    container = make_ingest_container(
        _doc(ingest_type="file"),
        unprotect_client=unprotect_mock,
        minio_bytes=b"original-fallback",
    )

    from ragent.workers import ingest as worker_mod

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        await worker_mod.ingest_pipeline_task("DOC-UP-1")

    loader_kwargs = container.ingest_pipeline.run.call_args[0][0]["loader"]
    assert loader_kwargs["content"] == "original-fallback"
