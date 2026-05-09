"""Phase F — async ingest failure exposes error_code + error_reason
(00_rule.md §API Error Honesty: "async task failures MUST persist
error_code + error_reason on the document row and the corresponding
GET /<resource>/{id} endpoint MUST return both fields alongside
status='FAILED'").
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.ingest import create_router


def _doc_row(**over):
    """Lightweight stand-in for DocumentRow in router tests."""
    base = MagicMock()
    base.document_id = over.get("document_id", "DOC-X")
    base.status = over.get("status", "FAILED")
    base.attempt = over.get("attempt", 1)
    base.updated_at = None
    base.ingest_type = "inline"
    base.minio_site = None
    base.source_id = "S"
    base.source_app = "A"
    base.source_title = "T"
    base.source_url = None
    base.error_code = over.get("error_code")
    base.error_reason = over.get("error_reason")
    return base


def test_get_ingest_returns_error_code_and_reason_on_failed():
    svc = MagicMock()
    svc.get = AsyncMock(
        return_value=_doc_row(
            document_id="DOC-FAIL",
            status="FAILED",
            error_code="EMBEDDER_ERROR",
            error_reason="UpstreamServiceError: embedding failed after retries: HTTP 503",
        )
    )

    app = FastAPI()
    app.include_router(create_router(svc))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ingest/DOC-FAIL")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "EMBEDDER_ERROR"
    assert "embedding failed" in body["error_reason"]


def test_get_ingest_returns_null_error_fields_on_success():
    """Non-failure rows MUST still serialize the keys (with None) so a
    schema-aware downstream API can treat them as documented."""
    svc = MagicMock()
    svc.get = AsyncMock(return_value=_doc_row(status="READY"))

    app = FastAPI()
    app.include_router(create_router(svc))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ingest/DOC-OK")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "READY"
    assert body["error_code"] is None
    assert body["error_reason"] is None


def test_document_row_carries_error_fields():
    """DocumentRow.from_mapping pulls error_code + error_reason from the row."""
    from ragent.repositories.document_repository import DocumentRow

    row = DocumentRow.from_mapping(
        {
            "document_id": "X",
            "create_user": "u",
            "source_id": "S",
            "source_app": "A",
            "source_title": "T",
            "source_meta": None,
            "object_key": "k",
            "status": "FAILED",
            "attempt": 1,
            "created_at": None,
            "updated_at": None,
            "ingest_type": "inline",
            "minio_site": None,
            "source_url": None,
            "mime_type": "text/plain",
            "error_code": "PIPELINE_TIMEOUT_AGGREGATE",
            "error_reason": "aggregate pipeline timeout after 300.0s",
        }
    )
    assert row.error_code == "PIPELINE_TIMEOUT_AGGREGATE"
    assert "300.0" in row.error_reason


def test_update_status_persists_error_code_and_reason():
    """Repository.update_status accepts error_code + error_reason and emits
    them in the SQL UPDATE bind params."""
    import asyncio

    from ragent.repositories.document_repository import DocumentRepository

    captured: dict = {}

    fake_result = MagicMock()
    fake_result.rowcount = 1

    class _Conn:
        async def execute(self, _stmt, params):
            captured["params"] = params
            return fake_result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Engine:
        def begin(self):
            return _Conn()

    repo = DocumentRepository(engine=_Engine())

    asyncio.run(
        repo.update_status(
            "DOC-X",
            from_status="PENDING",
            to_status="FAILED",
            error_code="EMBEDDER_ERROR",
            error_reason="boom" * 100,  # 400 chars; must be truncated to 255
        )
    )
    assert captured["params"]["error_code"] == "EMBEDDER_ERROR"
    # Truncation guard: never exceed VARCHAR(255).
    assert len(captured["params"]["error_reason"]) == 255


def test_update_status_omits_error_clause_when_not_provided():
    """Backwards compat — existing callers that don't pass error_code/reason
    still produce a working UPDATE without binding the new params."""
    import asyncio

    from ragent.repositories.document_repository import DocumentRepository

    captured: dict = {}

    fake_result = MagicMock()
    fake_result.rowcount = 1

    class _Conn:
        async def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return fake_result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Engine:
        def begin(self):
            return _Conn()

    repo = DocumentRepository(engine=_Engine())
    asyncio.run(repo.update_status("DOC-X", from_status="PENDING", to_status="READY"))
    assert "error_code" not in captured["params"]
    assert "error_code = " not in captured["sql"]
