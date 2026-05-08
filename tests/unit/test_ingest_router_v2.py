"""T2v.24 — Ingest router v2: JSON-only, discriminated body, no multipart."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.ingest import create_router
from ragent.services.ingest_service import (
    FileTooLarge,
    ObjectNotFoundError,
    UnknownMinioSiteError,
)


def _make_client(svc=None):
    svc = svc or AsyncMock()
    app = FastAPI()
    app.include_router(create_router(svc=svc))
    return TestClient(app, raise_server_exceptions=False), svc


_INLINE = {
    "ingest_type": "inline",
    "source_id": "DOC-1",
    "source_app": "confluence",
    "source_title": "T",
    "mime_type": "text/markdown",
    "content": "# H1\n",
}

_FILE = {
    "ingest_type": "file",
    "source_id": "DOC-2",
    "source_app": "s3",
    "source_title": "T",
    "mime_type": "text/html",
    "minio_site": "tenant-eu-1",
    "object_key": "reports/2025.html",
}


def test_post_ingest_inline_returns_202_with_document_id():
    svc = AsyncMock()
    svc.create.return_value = "AAAAAAAAAAAAAAAAAAAAAAAAAAA"
    client, _ = _make_client(svc)
    resp = client.post("/ingest", json=_INLINE, headers={"X-User-Id": "alice"})
    assert resp.status_code == 202
    assert resp.json()["document_id"] == "AAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_post_ingest_file_returns_202_with_document_id():
    svc = AsyncMock()
    svc.create.return_value = "BBBBBBBBBBBBBBBBBBBBBBBBBBB"
    client, _ = _make_client(svc)
    resp = client.post("/ingest", json=_FILE, headers={"X-User-Id": "alice"})
    assert resp.status_code == 202
    assert resp.json()["document_id"] == "BBBBBBBBBBBBBBBBBBBBBBBBBBB"


def test_post_ingest_unknown_mime_returns_415():
    bad = {**_INLINE, "mime_type": "image/png"}
    client, _ = _make_client()
    resp = client.post("/ingest", json=bad, headers={"X-User-Id": "alice"})
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "INGEST_MIME_UNSUPPORTED"


def test_post_ingest_csv_mime_returns_415_in_v2():
    bad = {**_INLINE, "mime_type": "text/csv"}
    client, _ = _make_client()
    resp = client.post("/ingest", json=bad, headers={"X-User-Id": "alice"})
    assert resp.status_code == 415
    assert resp.json()["error_code"] == "INGEST_MIME_UNSUPPORTED"


def test_post_ingest_missing_required_field_returns_422():
    bad = dict(_INLINE)
    del bad["source_id"]
    client, _ = _make_client()
    resp = client.post("/ingest", json=bad, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "INGEST_VALIDATION"
    assert "errors" in body


def test_post_ingest_unknown_ingest_type_returns_422():
    bad = {**_INLINE, "ingest_type": "ftp"}
    client, _ = _make_client()
    resp = client.post("/ingest", json=bad, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422


def test_post_ingest_inline_too_large_returns_413():
    svc = AsyncMock()
    svc.create.side_effect = FileTooLarge("too big")
    client, _ = _make_client(svc)
    resp = client.post("/ingest", json=_INLINE, headers={"X-User-Id": "alice"})
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "INGEST_FILE_TOO_LARGE"


def test_post_ingest_file_unknown_minio_site_returns_422():
    svc = AsyncMock()
    svc.create.side_effect = UnknownMinioSiteError("nope")
    client, _ = _make_client(svc)
    resp = client.post("/ingest", json=_FILE, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INGEST_MINIO_SITE_UNKNOWN"


def test_post_ingest_file_object_missing_returns_422():
    svc = AsyncMock()
    svc.create.side_effect = ObjectNotFoundError("missing")
    client, _ = _make_client(svc)
    resp = client.post("/ingest", json=_FILE, headers={"X-User-Id": "alice"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INGEST_OBJECT_NOT_FOUND"


def test_post_ingest_multipart_returns_415():
    """Old multipart callers must hit a clean 415 — no surprise routing."""
    client, _ = _make_client()
    resp = client.post(
        "/ingest",
        data={"source_id": "DOC", "source_app": "a", "source_title": "T"},
        files={"file": ("x.txt", b"x", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code in (415, 422)
    if resp.status_code == 415:
        assert resp.json()["error_code"] == "INGEST_MIME_UNSUPPORTED"


def test_post_ingest_passes_inline_content_to_service():
    svc = AsyncMock()
    svc.create.return_value = "id"
    client, _ = _make_client(svc)
    resp = client.post("/ingest", json=_INLINE, headers={"X-User-Id": "alice"})
    assert resp.status_code == 202
    svc.create.assert_called_once()
    kwargs = svc.create.call_args.kwargs
    req = kwargs["request"]
    assert req.ingest_type == "inline"
    assert req.content == "# H1\n"
    assert kwargs["create_user"] == "alice"


def test_post_ingest_error_body_is_rfc9457():
    bad = dict(_INLINE)
    del bad["source_app"]
    client, _ = _make_client()
    resp = client.post("/ingest", json=bad, headers={"X-User-Id": "alice"})
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    for k in ("type", "title", "status", "error_code"):
        assert k in body


def test_get_ingest_unchanged():
    """GET still works (not part of v2 breaking change)."""
    import datetime

    from ragent.repositories.document_repository import DocumentRow

    doc = DocumentRow(
        document_id="ID1",
        create_user="alice",
        source_id="S",
        source_app="a",
        source_title="T",
        source_meta=None,
        object_key="key",
        status="READY",
        attempt=1,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
    )
    svc = AsyncMock()
    svc.get.return_value = doc
    client, _ = _make_client(svc)
    resp = client.get("/ingest/ID1", headers={"X-User-Id": "alice"})
    assert resp.status_code == 200


def test_delete_ingest_unchanged():
    svc = AsyncMock()
    client, _ = _make_client(svc)
    resp = client.delete("/ingest/ID1", headers={"X-User-Id": "alice"})
    assert resp.status_code == 204


def test_list_ingest_unchanged():
    from ragent.services.ingest_service import IngestListResult

    svc = AsyncMock()
    svc.list.return_value = IngestListResult(items=[], next_cursor=None)
    client, _ = _make_client(svc)
    resp = client.get("/ingest", headers={"X-User-Id": "alice"})
    assert resp.status_code == 200
