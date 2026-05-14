"""IngestService.rerun — manual rerun for non-READY documents (spec §3.1.x)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ragent.services.ingest_service import (
    DocumentNotFound,
    DocumentNotRerunnable,
    IngestService,
)


def _make_service():
    repo = AsyncMock()
    broker = AsyncMock()
    svc = IngestService(repo=repo, storage=MagicMock(), broker=broker, registry=MagicMock())
    return svc, repo, broker


async def test_rerun_marks_then_enqueues_in_order():
    svc, repo, broker = _make_service()
    repo.mark_for_rerun.return_value = "ok"

    order: list[str] = []
    repo.mark_for_rerun.side_effect = lambda *_a, **_k: order.append("mark") or "ok"
    broker.enqueue.side_effect = lambda *_a, **_k: order.append("enqueue")

    await svc.rerun("DOC1")
    assert order == ["mark", "enqueue"]
    broker.enqueue.assert_awaited_once_with("ingest.pipeline", document_id="DOC1")


async def test_rerun_raises_not_found_and_does_not_enqueue():
    svc, repo, broker = _make_service()
    repo.mark_for_rerun.return_value = "not_found"

    with pytest.raises(DocumentNotFound):
        await svc.rerun("MISSING")
    broker.enqueue.assert_not_called()


async def test_rerun_raises_not_rerunnable_and_does_not_enqueue():
    svc, repo, broker = _make_service()
    repo.mark_for_rerun.return_value = "not_rerunnable"

    with pytest.raises(DocumentNotRerunnable):
        await svc.rerun("READY-DOC")
    broker.enqueue.assert_not_called()
