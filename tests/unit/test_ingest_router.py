"""T2.13 — IngestRouter: validation, error codes, RFC 9457 problem+json (S23, B5, B11)."""

import datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from ragent.routers.ingest import create_router
from ragent.services.ingest_service import FileTooLarge, IngestListResult, MimeNotAllowed


def _dt():
    return datetime.datetime.now(datetime.UTC)


def _make_client(svc=None):
    from fastapi import FastAPI

    svc = svc or MagicMock()
    app = FastAPI()
    router = create_router(svc=svc)
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), svc


def test_post_ingest_returns_202_with_task_id():
    svc = MagicMock()
    svc.create.return_value = "AAAAAAAAAAAAAAAAAAAAAAAAAAA"
    client, _ = _make_client(svc)
    resp = client.post(
        "/ingest",
        data={"source_id": "DOC-1", "source_app": "confluence", "source_title": "My Title"},
        files={"file": ("test.txt", b"hello world", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"] == "AAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_post_ingest_missing_source_id_returns_422():
    client, _ = _make_client()
    resp = client.post(
        "/ingest",
        data={"source_app": "confluence", "source_title": "T"},
        files={"file": ("test.txt", b"x", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body.get("error_code") == "INGEST_VALIDATION"
    assert "errors" in body


def test_post_ingest_missing_source_app_returns_422():
    client, _ = _make_client()
    resp = client.post(
        "/ingest",
        data={"source_id": "S1", "source_title": "T"},
        files={"file": ("test.txt", b"x", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body.get("error_code") == "INGEST_VALIDATION"


def test_post_ingest_missing_source_title_returns_422():
    client, _ = _make_client()
    resp = client.post(
        "/ingest",
        data={"source_id": "S1", "source_app": "app"},
        files={"file": ("test.txt", b"x", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body.get("error_code") == "INGEST_VALIDATION"


def test_post_ingest_unsupported_mime_returns_415():
    svc = MagicMock()
    svc.create.side_effect = MimeNotAllowed("image/png")
    client, _ = _make_client(svc)
    resp = client.post(
        "/ingest",
        data={"source_id": "S1", "source_app": "app", "source_title": "T"},
        files={"file": ("test.png", b"x", "image/png")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 415
    body = resp.json()
    assert body["error_code"] == "INGEST_MIME_UNSUPPORTED"


def test_post_ingest_file_too_large_returns_413():
    svc = MagicMock()
    svc.create.side_effect = FileTooLarge("too big")
    client, _ = _make_client(svc)
    resp = client.post(
        "/ingest",
        data={"source_id": "S1", "source_app": "app", "source_title": "T"},
        files={"file": ("test.txt", b"x", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["error_code"] == "INGEST_FILE_TOO_LARGE"


def test_post_ingest_error_body_is_rfc9457():
    client, _ = _make_client()
    resp = client.post(
        "/ingest",
        data={"source_app": "confluence", "source_title": "T"},
        files={"file": ("test.txt", b"x", "text/plain")},
        headers={"X-User-Id": "alice"},
    )
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert "type" in body
    assert "title" in body
    assert "status" in body
    assert "error_code" in body


def test_get_ingest_by_id():
    from ragent.repositories.document_repository import DocumentRow

    doc = DocumentRow(
        document_id="ID1",
        create_user="alice",
        source_id="S",
        source_app="app",
        source_title="T",
        source_workspace=None,
        object_key="key",
        status="READY",
        attempt=1,
        created_at=_dt(),
        updated_at=_dt(),
    )
    svc = MagicMock()
    svc.get.return_value = doc
    client, _ = _make_client(svc)
    resp = client.get("/ingest/ID1", headers={"X-User-Id": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "READY"
    assert body["attempt"] == 1


def test_get_ingest_by_id_not_found():
    svc = MagicMock()
    svc.get.return_value = None
    client, _ = _make_client(svc)
    resp = client.get("/ingest/NOTEXIST", headers={"X-User-Id": "alice"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "INGEST_NOT_FOUND"


def test_delete_ingest_returns_204():
    svc = MagicMock()
    svc.delete.return_value = None
    client, _ = _make_client(svc)
    resp = client.delete("/ingest/ID1", headers={"X-User-Id": "alice"})
    assert resp.status_code == 204


def test_list_ingest_returns_200():
    svc = MagicMock()
    svc.list.return_value = IngestListResult(items=[], next_cursor=None)
    client, _ = _make_client(svc)
    resp = client.get("/ingest", headers={"X-User-Id": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["next_cursor"] is None


def test_missing_x_user_id_returns_422():
    from fastapi import FastAPI

    from ragent.bootstrap.app import _x_user_id_middleware

    svc = MagicMock()
    app = FastAPI()
    app.include_router(create_router(svc=svc))
    _x_user_id_middleware(app)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/ingest",
        data={"source_id": "S1", "source_app": "app", "source_title": "T"},
        files={"file": ("test.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 422
