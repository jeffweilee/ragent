"""T-RR.11 (B40) — HTTP DELETE /ingest/{id} invokes PluginRegistry.fan_out_delete.

Spec §3.1 step 1 prescribes: claim DELETING → fan_out_delete → MinIO unstage
(if PENDING/UPLOADED) → row delete. Implementation pre-fix skipped fan_out
because IngestService._broker (a TaskiqDispatcher) does not expose
fan_out_delete; the introspection branch silently no-op'd cleanup, leaving
ES chunks until the reconciler swept them. This test pins the new wiring:
IngestService is given a PluginRegistry directly and calls
fan_out_delete(document_id) in the request scope.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.plugins.registry import PluginRegistry
from ragent.routers.ingest import create_router
from ragent.services.ingest_service import IngestService


class _RecordingPlugin:
    """Minimal ExtractorPlugin double that records delete calls."""

    name = "recording-vector"
    required = True

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.extracted: list[str] = []

    def extract(self, document_id: str) -> None:
        self.extracted.append(document_id)

    def delete(self, document_id: str) -> None:
        self.deleted.append(document_id)


def _make_doc(document_id: str = "DOC123", status: str = "READY"):
    doc = MagicMock()
    doc.document_id = document_id
    doc.status = status
    doc.ingest_type = "inline"
    doc.minio_site = None
    doc.object_key = f"k_{document_id}"
    return doc


def _make_app(svc: IngestService) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(svc=svc))
    return TestClient(app)


def test_http_delete_invokes_fan_out_delete_once_for_ready_doc() -> None:
    """B40: DELETE on a READY doc fans out plugin.delete and hard-deletes the row."""
    repo = AsyncMock()
    repo.claim_for_deletion.return_value = _make_doc(status="READY")
    repo.delete = AsyncMock()

    storage = MagicMock()

    registry = PluginRegistry()
    plugin = _RecordingPlugin()
    registry.register(plugin)  # type: ignore[arg-type]

    svc = IngestService(repo=repo, storage=storage, broker=MagicMock(), registry=registry)
    client = _make_app(svc)

    resp = client.delete("/ingest/v1/DOC123", headers={"X-User-Id": "alice"})

    assert resp.status_code == 204
    assert plugin.deleted == ["DOC123"], "fan_out_delete must dispatch plugin.delete exactly once"
    repo.delete.assert_awaited_once_with("DOC123")


def test_delete_runs_fan_out_before_row_delete() -> None:
    """Cleanup ordering: ES chunks must be purged before the row vanishes,
    otherwise B36's hydrator drop is the only thing keeping the chunks
    invisible — disk reclaim still depends on the reconciler."""
    repo = AsyncMock()
    repo.claim_for_deletion.return_value = _make_doc(status="READY")

    call_order: list[str] = []

    async def _fake_repo_delete(doc_id: str) -> None:
        call_order.append(f"repo.delete:{doc_id}")

    repo.delete = AsyncMock(side_effect=_fake_repo_delete)

    registry = MagicMock()

    async def _fake_fan_out(doc_id: str) -> list:
        call_order.append(f"fan_out_delete:{doc_id}")
        return []

    registry.fan_out_delete = AsyncMock(side_effect=_fake_fan_out)

    svc = IngestService(repo=repo, storage=MagicMock(), broker=MagicMock(), registry=registry)
    client = _make_app(svc)

    resp = client.delete("/ingest/v1/DOCX", headers={"X-User-Id": "alice"})

    assert resp.status_code == 204
    assert call_order == ["fan_out_delete:DOCX", "repo.delete:DOCX"]


def test_delete_skips_fan_out_when_lock_unavailable() -> None:
    """LockNotAvailable (concurrent DELETE) → silent 204, no fan-out, no row delete."""
    from ragent.repositories.document_repository import LockNotAvailable

    repo = AsyncMock()
    repo.claim_for_deletion.side_effect = LockNotAvailable("locked")
    repo.delete = AsyncMock()

    registry = MagicMock()
    registry.fan_out_delete = AsyncMock()

    svc = IngestService(repo=repo, storage=MagicMock(), broker=MagicMock(), registry=registry)
    client = _make_app(svc)

    resp = client.delete("/ingest/v1/DOC1", headers={"X-User-Id": "alice"})

    assert resp.status_code == 204
    registry.fan_out_delete.assert_not_called()
    repo.delete.assert_not_called()
