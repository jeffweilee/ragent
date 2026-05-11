"""TDD — IngestService.create_from_upload: multipart binary staging path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.schemas.ingest import IngestMime
from ragent.services.ingest_service import FileTooLarge, IngestService


def _registry(*, default_put_key: str = "app_sid_DOC"):
    reg = MagicMock()
    reg.put_object_default.return_value = default_put_key
    return reg


def _service(registry=None):
    repo = AsyncMock()
    broker = AsyncMock()
    reg = registry or _registry()
    svc = IngestService(repo=repo, storage=reg, broker=broker, registry=MagicMock())
    return svc, repo, reg, broker


_DATA = b"# Hello\nworld\n"
_MIME = IngestMime.TEXT_MARKDOWN


async def test_create_from_upload_returns_document_id():
    svc, _, _, _ = _service()
    doc_id = await svc.create_from_upload(
        create_user="admin",
        source_id="doc-1",
        source_app="upload-cli",
        source_title="My Doc",
        mime_type=_MIME,
        data=_DATA,
    )
    assert len(doc_id) == 26


async def test_create_from_upload_calls_put_object_default():
    svc, _, reg, _ = _service()
    await svc.create_from_upload(
        create_user="admin",
        source_id="doc-1",
        source_app="upload-cli",
        source_title="My Doc",
        mime_type=_MIME,
        data=_DATA,
    )
    reg.put_object_default.assert_called_once()
    call_kwargs = reg.put_object_default.call_args.kwargs
    assert call_kwargs["source_app"] == "upload-cli"
    assert call_kwargs["source_id"] == "doc-1"
    assert call_kwargs["length"] == len(_DATA)
    assert call_kwargs["content_type"] == "text/markdown"


async def test_create_from_upload_records_inline_ingest_type():
    svc, repo, _, _ = _service()
    await svc.create_from_upload(
        create_user="admin",
        source_id="doc-1",
        source_app="upload-cli",
        source_title="My Doc",
        mime_type=_MIME,
        data=_DATA,
    )
    repo.create.assert_called_once()
    kwargs = repo.create.call_args.kwargs
    assert kwargs["ingest_type"] == "inline"
    assert kwargs["minio_site"] is None
    assert kwargs["mime_type"] == "text/markdown"


async def test_create_from_upload_enqueues_pipeline_task():
    svc, _, _, broker = _service()
    doc_id = await svc.create_from_upload(
        create_user="admin",
        source_id="doc-1",
        source_app="upload-cli",
        source_title="My Doc",
        mime_type=_MIME,
        data=_DATA,
    )
    broker.enqueue.assert_called_once()
    assert doc_id in str(broker.enqueue.call_args)


async def test_create_from_upload_persists_optional_fields():
    svc, repo, _, _ = _service()
    await svc.create_from_upload(
        create_user="admin",
        source_id="doc-1",
        source_app="upload-cli",
        source_title="My Doc",
        mime_type=_MIME,
        data=_DATA,
        source_meta="eng-team",
        source_url="https://example.com/doc",
    )
    kwargs = repo.create.call_args.kwargs
    assert kwargs["source_meta"] == "eng-team"
    assert kwargs["source_url"] == "https://example.com/doc"


async def test_create_from_upload_raises_file_too_large():
    svc, _, _, _ = _service()
    with pytest.raises(FileTooLarge):
        await svc.create_from_upload(
            create_user="admin",
            source_id="doc-1",
            source_app="upload-cli",
            source_title="My Doc",
            mime_type=_MIME,
            data=b"x" * 100,
            max_upload_bytes=10,
        )


async def test_create_from_upload_stage_binary_does_not_encode_utf8():
    """Binary bytes must pass through as-is — no UTF-8 encode step."""
    svc, _, reg, _ = _service()
    binary_data = bytes(range(256))
    await svc.create_from_upload(
        create_user="admin",
        source_id="doc-1",
        source_app="upload-cli",
        source_title="My Doc",
        mime_type=IngestMime.TEXT_PLAIN,
        data=binary_data,
    )
    call_kwargs = reg.put_object_default.call_args.kwargs
    assert call_kwargs["length"] == 256


async def test_create_from_upload_emits_log_event():
    import structlog

    svc, _, _, _ = _service()
    with structlog.testing.capture_logs() as logs:
        await svc.create_from_upload(
            create_user="admin",
            source_id="doc-1",
            source_app="upload-cli",
            source_title="My Doc",
            mime_type=_MIME,
            data=_DATA,
        )
    events = [e["event"] for e in logs]
    assert "ingest.received" in events
