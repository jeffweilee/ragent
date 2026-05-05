"""T2.7 — IngestService.create: validation, MIME, MinIO, repo, kiq, rollback (S23, B11, C1)."""

import io
from unittest.mock import MagicMock

import pytest

from ragent.services.ingest_service import FileTooLarge, IngestService, MimeNotAllowed

ALLOWED_MIMES = ["text/plain", "text/markdown", "text/html", "text/csv"]


def _make_service(repo=None, storage=None, broker=None):
    repo = repo or MagicMock()
    storage = storage or MagicMock()
    broker = broker or MagicMock()
    storage.put_object.return_value = "app_sid_DOC"
    svc = IngestService(repo=repo, chunks=MagicMock(), storage=storage, broker=broker)
    return svc, repo, storage, broker


def test_create_happy_path_returns_document_id():
    svc, repo, storage, broker = _make_service()
    doc_id = svc.create(
        create_user="alice",
        source_id="DOC-1",
        source_app="confluence",
        source_title="My Title",
        file_data=io.BytesIO(b"hello"),
        file_size=5,
        content_type="text/plain",
    )
    assert len(doc_id) == 26
    repo.create.assert_called_once()
    storage.put_object.assert_called_once()
    broker.enqueue.assert_called_once()


def test_create_persists_source_fields():
    svc, repo, storage, _ = _make_service()
    svc.create(
        create_user="bob",
        source_id="DOC-2",
        source_app="slack",
        source_title="Slack Doc",
        source_workspace="eng",
        file_data=io.BytesIO(b"data"),
        file_size=4,
        content_type="text/markdown",
    )
    kwargs = repo.create.call_args[1]
    assert kwargs["source_id"] == "DOC-2"
    assert kwargs["source_app"] == "slack"
    assert kwargs["source_title"] == "Slack Doc"
    assert kwargs["source_workspace"] == "eng"


def test_create_raises_on_unsupported_mime():
    svc, _, _, _ = _make_service()
    with pytest.raises(MimeNotAllowed):
        svc.create(
            create_user="alice",
            source_id="S",
            source_app="app",
            source_title="T",
            file_data=io.BytesIO(b"data"),
            file_size=4,
            content_type="image/png",
        )


def test_create_raises_on_file_too_large():
    svc, _, _, _ = _make_service()
    too_large = 52_428_801
    with pytest.raises(FileTooLarge):
        svc.create(
            create_user="alice",
            source_id="S",
            source_app="app",
            source_title="T",
            file_data=io.BytesIO(b"x"),
            file_size=too_large,
            content_type="text/plain",
        )


def test_create_rollback_row_if_minio_put_fails():
    repo = MagicMock()
    storage = MagicMock()
    storage.put_object.side_effect = Exception("MinIO down")
    broker = MagicMock()
    svc = IngestService(repo=repo, chunks=MagicMock(), storage=storage, broker=broker)

    with pytest.raises(Exception, match="MinIO down"):
        svc.create(
            create_user="alice",
            source_id="S",
            source_app="app",
            source_title="T",
            file_data=io.BytesIO(b"data"),
            file_size=4,
            content_type="text/plain",
        )
    # No row should persist (create never called, or delete called to clean up)
    assert repo.create.call_count == 0 or repo.delete.call_count >= 1


def test_create_dispatches_pipeline_task():
    svc, repo, _, broker = _make_service()
    doc_id = svc.create(
        create_user="alice",
        source_id="S",
        source_app="app",
        source_title="T",
        file_data=io.BytesIO(b"data"),
        file_size=4,
        content_type="text/plain",
    )
    broker.enqueue.assert_called_once()
    call_args = broker.enqueue.call_args
    # task name should reference ingest pipeline
    assert "ingest" in str(call_args).lower() or doc_id in str(call_args)


def test_create_stores_object_key_from_storage():
    svc, repo, storage, _ = _make_service()
    storage.put_object.return_value = "confluence_DOC-1_NEWID"
    svc.create(
        create_user="alice",
        source_id="DOC-1",
        source_app="confluence",
        source_title="T",
        file_data=io.BytesIO(b"data"),
        file_size=4,
        content_type="text/plain",
    )
    kwargs = repo.create.call_args[1]
    assert kwargs["object_key"] == "confluence_DOC-1_NEWID"


@pytest.mark.parametrize("mime", ALLOWED_MIMES)
def test_create_allows_all_p1_mimes(mime):
    svc, _, _, _ = _make_service()
    doc_id = svc.create(
        create_user="alice",
        source_id="S",
        source_app="app",
        source_title="T",
        file_data=io.BytesIO(b"data"),
        file_size=4,
        content_type=mime,
    )
    assert doc_id  # no exception raised
