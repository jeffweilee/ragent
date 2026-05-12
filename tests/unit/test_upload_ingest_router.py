"""TDD — POST /ingest/v1/upload: multipart file ingest endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.admin_ingest import create_router
from ragent.services.ingest_service import FileTooLarge

_DOC_ID = "AAAAAAAAAAAAAAAAAAAAAAAAAAA"

_FORM = {
    "source_id": "doc-1",
    "source_app": "upload-cli",
    "source_title": "My Doc",
    "mime_type": "text/markdown",
}

_FILE = ("file", ("report.md", b"# Hello\n", "text/markdown"))


def _make_client(svc=None):
    svc = svc or AsyncMock()
    app = FastAPI()
    app.include_router(create_router(svc=svc))
    return TestClient(app, raise_server_exceptions=False), svc


def test_upload_returns_202_with_document_id():
    svc = AsyncMock()
    svc.create_from_upload.return_value = _DOC_ID
    client, _ = _make_client(svc)
    resp = client.post(
        "/ingest/v1/upload",
        data=_FORM,
        files=[_FILE],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 202
    assert resp.json()["document_id"] == _DOC_ID


def test_upload_passes_correct_fields_to_service():
    svc = AsyncMock()
    svc.create_from_upload.return_value = _DOC_ID
    client, _ = _make_client(svc)
    client.post(
        "/ingest/v1/upload",
        data={**_FORM, "source_meta": "eng", "source_url": "https://example.com"},
        files=[_FILE],
        headers={"X-User-Id": "admin"},
    )
    svc.create_from_upload.assert_called_once()
    kwargs = svc.create_from_upload.call_args.kwargs
    assert kwargs["create_user"] == "admin"
    assert kwargs["source_id"] == "doc-1"
    assert kwargs["source_app"] == "upload-cli"
    assert kwargs["source_title"] == "My Doc"
    assert kwargs["source_meta"] == "eng"
    assert kwargs["source_url"] == "https://example.com"
    assert kwargs["data"] == b"# Hello\n"


def test_upload_file_too_large_returns_413():
    svc = AsyncMock()
    svc.create_from_upload.side_effect = FileTooLarge("too big")
    client, _ = _make_client(svc)
    resp = client.post(
        "/ingest/v1/upload",
        data=_FORM,
        files=[_FILE],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "INGEST_FILE_TOO_LARGE"


def test_upload_invalid_mime_type_returns_422():
    client, _ = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_FORM, "mime_type": "image/png"},
        files=[_FILE],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 422


def test_upload_missing_required_field_returns_422():
    client, _ = _make_client()
    bad_form = {k: v for k, v in _FORM.items() if k != "source_id"}
    resp = client.post(
        "/ingest/v1/upload",
        data=bad_form,
        files=[_FILE],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 422


def test_upload_missing_file_returns_422():
    client, _ = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data=_FORM,
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 422


def test_upload_file_too_large_via_size_attr_returns_413(monkeypatch):
    """Early rejection via file.size avoids reading the payload into memory."""
    import ragent.routers.admin_ingest as mod

    monkeypatch.setattr(mod, "_MAX_UPLOAD_BYTES", 5)
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data=_FORM,
        files=[("file", ("big.md", b"x" * 10, "text/plain"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "INGEST_FILE_TOO_LARGE"
    svc.create_from_upload.assert_not_called()


def test_upload_pptx_alias_accepted():
    """Short alias 'pptx' is normalised to the full MIME by IngestMime._missing_."""
    from ragent.schemas.ingest import IngestMime

    svc = AsyncMock()
    svc.create_from_upload.return_value = _DOC_ID
    client, _ = _make_client(svc)
    resp = client.post(
        "/ingest/v1/upload",
        data={**_FORM, "mime_type": "pptx"},
        files=[("file", ("deck.pptx", b"PK\x03\x04", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 202
    kwargs = svc.create_from_upload.call_args.kwargs
    assert kwargs["mime_type"] == IngestMime.PPTX


def test_upload_optional_fields_default_to_none():
    svc = AsyncMock()
    svc.create_from_upload.return_value = _DOC_ID
    client, _ = _make_client(svc)
    client.post(
        "/ingest/v1/upload",
        data=_FORM,
        files=[_FILE],
        headers={"X-User-Id": "admin"},
    )
    kwargs = svc.create_from_upload.call_args.kwargs
    assert kwargs["source_meta"] is None
    assert kwargs["source_url"] is None
