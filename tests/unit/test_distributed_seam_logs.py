"""Phase C — distributed-seam business logs (00_rule.md §Service Boundary Logs).

Six seams that previously had no operator-greppable event:

- ``ingest.dispatched``    — producer side after broker.enqueue
- ``ingest.task.started``  — worker side after claim_for_processing
- ``ingest.deleted``       — service-level delete tail
- ``supersede.completed``  — supersede task tail
- ``chat.hydrator.dropped``— silent READY-filter drop count
- ``documents.status.transition`` — repository status mutations

Each test uses ``structlog.testing.capture_logs`` (not ``capsys``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from haystack.dataclasses import Document

from ragent.schemas.ingest import InlineIngestRequest
from ragent.services.ingest_service import IngestService


def _registry():
    reg = MagicMock()
    reg.put_object_default.return_value = "app_DOC-1_id"
    reg.stat_object.return_value = 100
    return reg


def _service(repo=None, broker=None):
    repo = repo or AsyncMock()
    broker = broker or AsyncMock()
    svc = IngestService(repo=repo, storage=_registry(), broker=broker, registry=MagicMock())
    return svc, repo, broker


def _inline_req() -> InlineIngestRequest:
    return InlineIngestRequest(
        ingest_type="inline",
        source_id="DOC-1",
        source_app="app",
        source_title="T",
        mime_type="text/markdown",
        content="# H\n",
    )


# ---------------------------------------------------------------------------
# Seam 1: ingest.dispatched (producer side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_dispatched_fires_after_enqueue():
    svc, _, _ = _service()
    with structlog.testing.capture_logs() as logs:
        await svc.create(create_user="u", request=_inline_req())
    events = [e["event"] for e in logs]
    assert "ingest.dispatched" in events, f"missing ingest.dispatched: events={events}"
    dispatched = next(e for e in logs if e["event"] == "ingest.dispatched")
    assert dispatched.get("source_app") == "app"
    assert dispatched.get("source_id") == "DOC-1"
    assert dispatched.get("document_id")  # crockford id, just non-empty


# ---------------------------------------------------------------------------
# Seam 2: ingest.task.started (consumer side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_task_started_fires_after_claim(monkeypatch: pytest.MonkeyPatch):
    """Worker emits ingest.task.started right after a successful claim."""
    from ragent.workers import ingest as worker_mod

    doc = MagicMock()
    doc.minio_site = "__default__"
    doc.object_key = "k"
    doc.source_id = "S"
    doc.source_app = "A"
    doc.source_url = None
    doc.source_title = None
    doc.source_meta = None
    doc.mime_type = "text/markdown"
    doc.ingest_type = "inline"

    repo = MagicMock()
    repo.claim_for_processing = AsyncMock(return_value=doc)
    repo.update_status = AsyncMock()
    repo.promote_to_ready_and_demote_siblings = AsyncMock(return_value=True)

    registry = MagicMock()
    registry.head_object.return_value = (4, "text/markdown")
    registry.get_object.return_value = b"text"

    container = MagicMock()
    container.doc_repo = repo
    container.minio_registry = registry
    container.ingest_pipeline.run.return_value = {"writer": {"documents_written": 1}}
    container.registry = MagicMock()
    container.registry.fan_out = AsyncMock()
    container.embedding_registry.refresh = AsyncMock()

    monkeypatch.setattr(
        "ragent.bootstrap.composition.get_container", lambda: container, raising=False
    )

    with structlog.testing.capture_logs() as logs:
        await worker_mod.ingest_pipeline_task("DOC-X")

    events = [e["event"] for e in logs]
    assert "ingest.task.started" in events, f"missing ingest.task.started: events={events}"
    started = next(e for e in logs if e["event"] == "ingest.task.started")
    assert started.get("document_id") == "DOC-X"
    assert started.get("source_app") == "A"


# ---------------------------------------------------------------------------
# Seam 3: ingest.deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_deleted_fires_after_delete():
    repo = AsyncMock()
    doc = MagicMock(status="UPLOADED", source_app="A", source_id="DOC-1", ingest_type="inline")
    doc.object_key = "k"
    doc.minio_site = "__default__"
    repo.claim_for_deletion.return_value = doc
    repo.delete = AsyncMock()
    registry = MagicMock()
    registry.fan_out_delete = AsyncMock()
    storage = MagicMock()

    svc = IngestService(repo=repo, storage=storage, broker=MagicMock(), registry=registry)

    with structlog.testing.capture_logs() as logs:
        await svc.delete("DOC-X")
    events = [e["event"] for e in logs]
    assert "ingest.deleted" in events, f"missing ingest.deleted: events={events}"
    deleted = next(e for e in logs if e["event"] == "ingest.deleted")
    assert deleted.get("document_id") == "DOC-X"


# ---------------------------------------------------------------------------
# Seam 4: supersede.completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_completed_fires_after_task(monkeypatch: pytest.MonkeyPatch):
    from ragent.workers import ingest as worker_mod

    container = MagicMock()
    container.doc_repo = AsyncMock()
    # service.supersede() loops on pop_oldest_loser_for_supersede until None.
    container.doc_repo.pop_oldest_loser_for_supersede = AsyncMock(return_value=None)
    container.minio_registry = MagicMock()
    container.registry = MagicMock()
    container.registry.fan_out_delete = AsyncMock()
    monkeypatch.setattr(
        "ragent.bootstrap.composition.get_container", lambda: container, raising=False
    )

    with structlog.testing.capture_logs() as logs:
        await worker_mod.ingest_supersede_task("SURV-1", "DOC-1", "app")
    events = [e["event"] for e in logs]
    assert "supersede.completed" in events, f"missing supersede.completed: events={events}"
    completed = next(e for e in logs if e["event"] == "supersede.completed")
    assert completed.get("survivor_id") == "SURV-1"
    assert completed.get("source_id") == "DOC-1"
    assert completed.get("source_app") == "app"


# ---------------------------------------------------------------------------
# Seam 5: chat.hydrator.dropped
# ---------------------------------------------------------------------------


def test_hydrator_logs_dropped_count_when_chunks_filtered(monkeypatch: pytest.MonkeyPatch):
    """Hydrator MUST emit chat.hydrator.dropped with dropped_count when ES
    returns chunks whose document_id is not in the READY-only DB result."""
    from ragent.pipelines import chat as chat_mod

    repo = MagicMock()
    repo.get_sources_by_document_ids = lambda _ids: {"R1": ("app", "DOC-R", "Title")}

    monkeypatch.setattr(chat_mod.anyio.from_thread, "run", lambda fn, *args, **kw: fn(*args, **kw))

    docs = [
        Document(content="a", meta={"document_id": "R1"}),  # kept
        Document(content="b", meta={"document_id": "X1"}),  # dropped
        Document(content="c", meta={"document_id": "X2"}),  # dropped
    ]

    h = chat_mod._SourceHydrator(doc_repo=repo)
    with structlog.testing.capture_logs() as logs:
        result = h.run(docs)

    assert len(result["documents"]) == 1
    events = [e["event"] for e in logs]
    assert "chat.hydrator.dropped" in events, f"missing chat.hydrator.dropped: events={events}"
    dropped = next(e for e in logs if e["event"] == "chat.hydrator.dropped")
    assert dropped.get("dropped_count") == 2
    assert dropped.get("before_count") == 3
    assert dropped.get("after_count") == 1


def test_hydrator_does_not_log_when_no_drop(monkeypatch: pytest.MonkeyPatch):
    """No dropped_count event when every chunk passes the READY gate
    (avoid log spam on the happy path)."""
    from ragent.pipelines import chat as chat_mod

    repo = MagicMock()
    repo.get_sources_by_document_ids = lambda _ids: {
        "R1": ("app", "DOC-R", "Title"),
        "R2": ("app", "DOC-R", "Title"),
    }
    monkeypatch.setattr(chat_mod.anyio.from_thread, "run", lambda fn, *args, **kw: fn(*args, **kw))

    docs = [
        Document(content="a", meta={"document_id": "R1"}),
        Document(content="b", meta={"document_id": "R2"}),
    ]
    h = chat_mod._SourceHydrator(doc_repo=repo)
    with structlog.testing.capture_logs() as logs:
        h.run(docs)
    assert "chat.hydrator.dropped" not in [e["event"] for e in logs]


# ---------------------------------------------------------------------------
# Seam 6: documents.status.transition (repository)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_logs_status_transition(monkeypatch: pytest.MonkeyPatch):
    """update_status emits documents.status.transition with from/to status
    and the document_id (00_rule.md §Service Boundary Logs)."""
    from sqlalchemy.engine import Result

    from ragent.repositories.document_repository import DocumentRepository

    fake_result = MagicMock(spec=Result)
    fake_result.rowcount = 1

    class _FakeConn:
        async def execute(self, *_a, **_kw):
            return fake_result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

        def connect(self):
            return _FakeConn()

    repo = DocumentRepository(engine=_FakeEngine())

    # Bypass real assert_transition import — let it accept UPLOADED→PENDING.
    with structlog.testing.capture_logs() as logs:
        await repo.update_status("DOC-X", from_status="UPLOADED", to_status="PENDING")
    events = [e["event"] for e in logs]
    assert "documents.status.transition" in events, (
        f"missing documents.status.transition: events={events}"
    )
    transition = next(e for e in logs if e["event"] == "documents.status.transition")
    assert transition.get("document_id") == "DOC-X"
    assert transition.get("from_status") == "UPLOADED"
    assert transition.get("to_status") == "PENDING"
