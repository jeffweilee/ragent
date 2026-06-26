"""T-CAT.12 — Attachments upload and retrieval endpoints."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.repositories.attachment_repository import AttachmentRepository, AttachmentRow
from ragent.routers.attachments import create_attachments_router
from ragent.services.chat_attachment_service import ChatAttachmentService

_NOW = datetime.datetime(2026, 1, 1)


def _build_test_app_with_mocked_attachments() -> tuple[FastAPI, dict]:
    """Build app with mocked attachment service and repository."""
    service = AsyncMock(spec=ChatAttachmentService)
    repository = AsyncMock(spec=AttachmentRepository)

    app = FastAPI()
    app.include_router(create_attachments_router(service=service, repository=repository))

    return app, {"service": service, "repository": repository}


def test_attachment_routes_follow_versioning_contract() -> None:
    """POST/GET /chatagent/v3/attachments/* must match ^/[a-z][a-z0-9-]*/v[1-9]\d*"""
    app, _ = _build_test_app_with_mocked_attachments()

    with TestClient(app) as client:
        # Just verify routes exist and respond (404 is OK for missing endpoint)
        resp = client.get("/chatagent/v3/attachments?threadId=test")
        # Route exists, so status should be 200 or 422 (validation error), not 404
        assert resp.status_code != 404


def test_post_attachments_upload_stores_and_returns_id() -> None:
    """POST /chatagent/v3/attachments/upload stores file and returns attachment_id."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["service"].upload = AsyncMock(return_value="att_test_123")

    with TestClient(app) as client:
        file_bytes = b"test file content"

        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.txt", file_bytes)},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["attachmentId"] == "att_test_123"


def test_post_attachments_upload_requires_thread_id() -> None:
    """POST /chatagent/v3/attachments/upload requires threadId."""
    app, _ = _build_test_app_with_mocked_attachments()

    with TestClient(app) as client:
        file_bytes = b"test"

        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.txt", file_bytes)},
            headers={"X-User-Id": "alice"},
        )

        # Missing threadId should cause validation error
        assert resp.status_code in (400, 422)


def test_get_attachments_lists_by_thread() -> None:
    """GET /chatagent/v3/attachments?threadId= lists thread attachments."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].list_by_thread = AsyncMock(
        return_value=[
            AttachmentRow(
                attachment_id="att_1",
                thread_id="thread-1",
                create_user="alice",
                filename="test.txt",
                mime_type="text/plain",
                size_bytes=100,
                status="READY",
                created_at=_NOW,
                updated_at=_NOW,
            )
        ]
    )

    with TestClient(app) as client:
        resp = client.get(
            "/chatagent/v3/attachments?threadId=thread-1",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["attachments"]) == 1
        assert body["attachments"][0]["attachmentId"] == "att_1"


def test_post_attachments_upload_logs_request() -> None:
    """POST upload logs attachments.upload_request with thread/filename context."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["service"].upload = AsyncMock(return_value="att_test_123")

    with TestClient(app) as client, structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.txt", b"test file content")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    request_log = next(e for e in logs if e["event"] == "attachments.upload_request")
    assert request_log["thread_id"] == "thread-1"
    assert request_log["filename"] == "test.txt"
    assert request_log["user_id"] == "alice"


def test_post_attachments_upload_logs_rejected_mime() -> None:
    """POST upload with an unsupported MIME type logs a warning before the 415."""
    app, _ = _build_test_app_with_mocked_attachments()

    with TestClient(app) as client, structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.exe", b"x", "application/x-msdownload")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 415
    rejected = next(e for e in logs if e["event"] == "attachments.upload_rejected_mime")
    assert rejected["thread_id"] == "thread-1"
    assert rejected["mime_type"] == "application/x-msdownload"
    assert rejected["log_level"] == "warning"


def test_get_attachments_logs_list_request() -> None:
    """GET attachments logs attachments.list_request with thread context."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].list_by_thread = AsyncMock(return_value=[])

    with TestClient(app) as client, structlog.testing.capture_logs() as logs:
        resp = client.get(
            "/chatagent/v3/attachments?threadId=thread-1",
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 200
    request_log = next(e for e in logs if e["event"] == "attachments.list_request")
    assert request_log["thread_id"] == "thread-1"
    assert request_log["user_id"] == "alice"
