"""T-SEC.1 — Magic-byte rejection at POST /ingest/v1/upload.

Binary MIME (docx / pptx / pdf) uploads must carry the matching file
signature in their first bytes; mismatch is rejected 415
INGEST_MAGIC_MISMATCH before the bytes reach the worker pipeline.
Text MIMEs are not signature-checked (no fixed magic).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.admin_ingest import create_router

_DOC_ID = "AAAAAAAAAAAAAAAAAAAAAAAAAAA"

_BASE_FORM = {
    "source_id": "doc-1",
    "source_app": "upload-cli",
    "source_title": "My Doc",
}


def _make_client(svc=None):
    svc = svc or AsyncMock()
    svc.create_from_upload.return_value = _DOC_ID
    app = FastAPI()
    app.include_router(create_router(svc=svc))
    return TestClient(app, raise_server_exceptions=False), svc


def test_docx_with_wrong_magic_returns_415():
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "docx"},
        files=[("file", ("evil.docx", b"NOT_A_ZIP_AT_ALL", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "INGEST_MAGIC_MISMATCH"
    svc.create_from_upload.assert_not_called()


def test_pptx_with_wrong_magic_returns_415():
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "pptx"},
        files=[("file", ("evil.pptx", b"%PDF-1.4 fake", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "INGEST_MAGIC_MISMATCH"
    svc.create_from_upload.assert_not_called()


def test_pdf_with_wrong_magic_returns_415():
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "pdf"},
        files=[("file", ("evil.pdf", b"PK\x03\x04 fake zip", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "INGEST_MAGIC_MISMATCH"
    svc.create_from_upload.assert_not_called()


def test_docx_with_valid_zip_magic_passes_through():
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "docx"},
        files=[("file", ("ok.docx", b"PK\x03\x04rest_of_zip", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 202
    svc.create_from_upload.assert_called_once()


def test_pdf_with_valid_pdf_magic_passes_through():
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "pdf"},
        files=[("file", ("ok.pdf", b"%PDF-1.7\nrest", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 202
    svc.create_from_upload.assert_called_once()


def test_text_mime_bypasses_magic_check():
    """Text MIME types have no fixed signature; arbitrary bytes are accepted."""
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "text/plain"},
        files=[("file", ("note.txt", b"any bytes here", "text/plain"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 202
    svc.create_from_upload.assert_called_once()


def test_empty_binary_upload_returns_415():
    """A binary MIME with zero bytes cannot match any signature."""
    client, svc = _make_client()
    resp = client.post(
        "/ingest/v1/upload",
        data={**_BASE_FORM, "mime_type": "docx"},
        files=[("file", ("empty.docx", b"", "application/octet-stream"))],
        headers={"X-User-Id": "admin"},
    )
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "INGEST_MAGIC_MISMATCH"
    svc.create_from_upload.assert_not_called()
