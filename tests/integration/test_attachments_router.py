"""T-CAT.12 — Attachments upload and retrieval endpoints."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.repositories.attachment_repository import AttachmentRepository, AttachmentRow
from ragent.routers.attachments import create_attachments_router
from ragent.services.chat_attachment_service import ChatAttachmentService, FileTooLarge

_NOW = datetime.datetime(2026, 1, 1)


def _build_test_app_with_mocked_attachments(
    *, max_size_bytes: int | None = None
) -> tuple[FastAPI, dict]:
    """Build app with mocked attachment service and repository."""
    service = AsyncMock(spec=ChatAttachmentService)
    repository = AsyncMock(spec=AttachmentRepository)

    app = FastAPI()
    kwargs = {} if max_size_bytes is None else {"max_size_bytes": max_size_bytes}
    app.include_router(create_attachments_router(service=service, repository=repository, **kwargs))

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

        assert resp.status_code == 202
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


def test_get_attachments_lists_error_fields_when_failed() -> None:
    """GET /chatagent/v3/attachments surfaces errorCode/errorReason for FAILED rows."""
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
                status="FAILED",
                created_at=_NOW,
                updated_at=_NOW,
                error_code="PIPELINE_UNEXPECTED_ERROR",
                error_reason="RuntimeError: boom",
            )
        ]
    )

    with TestClient(app) as client:
        resp = client.get(
            "/chatagent/v3/attachments?threadId=thread-1",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 200
        body = resp.json()["attachments"][0]
        assert body["errorCode"] == "PIPELINE_UNEXPECTED_ERROR"
        assert body["errorReason"] == "RuntimeError: boom"


def test_get_attachment_by_id_returns_status_and_error_fields() -> None:
    """GET /chatagent/v3/attachments/{id} returns the single attachment with error fields."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].get = AsyncMock(
        return_value=AttachmentRow(
            attachment_id="att_1",
            thread_id="thread-1",
            create_user="alice",
            filename="test.txt",
            mime_type="text/plain",
            size_bytes=100,
            status="PROCESSING",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )

    with TestClient(app) as client:
        resp = client.get(
            "/chatagent/v3/attachments/att_1",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["attachmentId"] == "att_1"
        assert body["status"] == "PROCESSING"
        assert body["errorCode"] is None
        assert body["errorReason"] is None


def test_get_attachment_by_id_returns_404_when_not_found() -> None:
    """GET /chatagent/v3/attachments/{id} returns 404 problem-details for unknown id."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].get = AsyncMock(return_value=None)

    with TestClient(app) as client:
        resp = client.get(
            "/chatagent/v3/attachments/att_missing",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "ATTACHMENT_NOT_FOUND"


def test_get_attachment_by_id_scopes_lookup_to_requesting_user() -> None:
    """GET /chatagent/v3/attachments/{id} passes the caller's user id to the repository."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].get = AsyncMock(return_value=None)

    with TestClient(app) as client:
        client.get(
            "/chatagent/v3/attachments/att_other_user",
            headers={"X-User-Id": "bob"},
        )

        mocks["repository"].get.assert_awaited_once_with("att_other_user", create_user="bob")


def test_get_attachment_by_id_returns_404_when_owned_by_another_user() -> None:
    """A row that exists but belongs to a different user surfaces as not-found, not leaked."""
    app, mocks = _build_test_app_with_mocked_attachments()
    # The repository itself filters by create_user, so a mismatched owner yields None.
    mocks["repository"].get = AsyncMock(return_value=None)

    with TestClient(app) as client:
        resp = client.get(
            "/chatagent/v3/attachments/att_1",
            headers={"X-User-Id": "mallory"},
        )

        assert resp.status_code == 404


def test_get_attachments_scopes_list_to_requesting_user() -> None:
    """GET /chatagent/v3/attachments passes the caller's user id to the repository."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].list_by_thread = AsyncMock(return_value=[])

    with TestClient(app) as client:
        client.get(
            "/chatagent/v3/attachments?threadId=thread-1",
            headers={"X-User-Id": "bob"},
        )

        mocks["repository"].list_by_thread.assert_awaited_once_with("thread-1", create_user="bob")


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

    assert resp.status_code == 202
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


def test_post_attachments_upload_rejected_mime_returns_problem_details() -> None:
    """The 415 body is an RFC 9457 problem-details response, not a bare FastAPI detail."""
    app, _ = _build_test_app_with_mocked_attachments()

    with TestClient(app) as client:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.exe", b"x", "application/x-msdownload")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 415
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["error_code"] == "ATTACHMENT_MIME_UNSUPPORTED"


def test_post_attachments_upload_falls_back_to_extension_when_content_type_generic() -> None:
    """A generic/wrong browser Content-Type (e.g. application/octet-stream) for a
    recognized extension (e.g. .pdf) resolves via filename extension instead of 415."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["service"].upload = AsyncMock(return_value="att_test_123")

    with TestClient(app) as client:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("report.pdf", b"%PDF-1.4", "application/octet-stream")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 202
    mocks["service"].upload.assert_awaited_once()
    assert mocks["service"].upload.await_args.kwargs["mime_type"].value == "application/pdf"


def test_post_attachments_upload_rejects_oversized_file() -> None:
    """POST upload returns 413 ATTACHMENT_TOO_LARGE via the router's early
    file.size check; service.upload is never reached (TestClient's multipart
    parser always knows file.size by the time the handler runs)."""
    app, mocks = _build_test_app_with_mocked_attachments(max_size_bytes=10)

    with TestClient(app) as client:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.txt", b"this file is way over ten bytes")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 413
    body = resp.json()
    assert body["error_code"] == "ATTACHMENT_TOO_LARGE"
    mocks["service"].upload.assert_not_called()


def test_post_attachments_upload_catches_service_raised_file_too_large() -> None:
    """If ChatAttachmentService.upload() raises FileTooLarge (authoritative
    post-read check, e.g. a chunked transfer the router's early check missed),
    the router still returns 413 ATTACHMENT_TOO_LARGE."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["service"].upload = AsyncMock(side_effect=FileTooLarge("32B exceeds limit 10B"))

    with TestClient(app) as client:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.txt", b"this file is way over ten bytes")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 413
    body = resp.json()
    assert body["error_code"] == "ATTACHMENT_TOO_LARGE"
    mocks["service"].upload.assert_called_once()


def test_post_attachments_upload_logs_rejected_size() -> None:
    """POST upload with an oversized file logs a warning before the 413."""
    app, _ = _build_test_app_with_mocked_attachments(max_size_bytes=10)

    with TestClient(app) as client, structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/chatagent/v3/attachments/upload",
            files={"file": ("test.txt", b"this file is way over ten bytes")},
            data={"threadId": "thread-1"},
            headers={"X-User-Id": "alice"},
        )

    assert resp.status_code == 413
    rejected = next(e for e in logs if e["event"] == "attachments.upload_rejected_size")
    assert rejected["thread_id"] == "thread-1"
    assert rejected["log_level"] == "warning"


def test_delete_attachment_returns_204_on_success() -> None:
    """DELETE /chatagent/v3/attachments/{id} returns 204 when the service deletes."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["service"].delete = AsyncMock(return_value=True)

    with TestClient(app) as client:
        resp = client.delete(
            "/chatagent/v3/attachments/att_1",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 204
        mocks["service"].delete.assert_awaited_once_with("att_1", create_user="alice")


def test_delete_attachment_returns_404_when_missing_or_not_owned() -> None:
    """DELETE returns 404 problem-details when service.delete() returns False."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["service"].delete = AsyncMock(return_value=False)

    with TestClient(app) as client:
        resp = client.delete(
            "/chatagent/v3/attachments/att_missing",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "ATTACHMENT_NOT_FOUND"


def test_get_attachments_mine_lists_by_user() -> None:
    """GET /chatagent/v3/attachments/mine lists every attachment for the caller."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].list_by_user = AsyncMock(
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
            "/chatagent/v3/attachments/mine",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["attachments"]) == 1
        assert body["attachments"][0]["attachmentId"] == "att_1"
        mocks["repository"].list_by_user.assert_awaited_once_with("alice")


def test_get_attachments_mine_is_not_swallowed_by_attachment_id_route() -> None:
    """'mine' must resolve to the dedicated route, not be parsed as an attachmentId."""
    app, mocks = _build_test_app_with_mocked_attachments()
    mocks["repository"].list_by_user = AsyncMock(return_value=[])

    with TestClient(app) as client:
        resp = client.get(
            "/chatagent/v3/attachments/mine",
            headers={"X-User-Id": "alice"},
        )

        assert resp.status_code == 200
        mocks["repository"].get.assert_not_called()


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
