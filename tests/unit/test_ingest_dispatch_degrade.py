"""T-RG.5 — an unreachable queue must not fail the ingest request.

`IngestService` persists the `documents` row (status UPLOADED) *before* it
enqueues, and both `run_startup_sweep` and `run_maintenance_cycle` re-dispatch
stale UPLOADED rows. So a broker outage costs latency, never data: the honest
answer is the 202 the contract already promises, not a 500 that tells the caller
the upload was lost when it was not.

The distinction that matters is transport-vs-bug. A missing task registration
(`TaskNotRegisteredError`) is a deployment defect no sweep will ever heal, so it
must keep surfacing loudly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import redis as redis_lib
from structlog.testing import capture_logs
from taskiq import AsyncBroker
from taskiq.decor import AsyncTaskiqDecoratedTask

from ragent.bootstrap.dispatcher import (
    TaskDispatchUnavailable,
    TaskiqDispatcher,
    TaskNotRegisteredError,
)
from ragent.schemas.ingest import InlineIngestRequest
from ragent.services.ingest_service import IngestService


def _registry():
    reg = MagicMock()
    reg.put_object_default.return_value = "app_sid_DOC"
    return reg


def _inline() -> InlineIngestRequest:
    return InlineIngestRequest(
        ingest_type="inline",
        source_id="DOC-1",
        source_app="app",
        source_title="t",
        mime_type="text/plain",
        content="hello",
    )


def _service(broker):
    repo = AsyncMock()
    return IngestService(repo=repo, storage=_registry(), broker=broker, registry=MagicMock()), repo


# --- dispatcher: transport failures become a typed, catchable error ----


async def test_dispatcher_maps_a_broker_outage_to_task_dispatch_unavailable() -> None:
    task = MagicMock(spec=AsyncTaskiqDecoratedTask)
    task.kiq = AsyncMock(side_effect=redis_lib.ConnectionError("connection refused"))
    broker = MagicMock(spec=AsyncBroker)
    broker.find_task.return_value = task

    with pytest.raises(TaskDispatchUnavailable):
        await TaskiqDispatcher(broker).enqueue("ingest.pipeline", document_id="d1")


async def test_dispatcher_still_raises_for_an_unregistered_task() -> None:
    """A deployment bug, not an outage — no sweep will ever heal it."""
    broker = MagicMock(spec=AsyncBroker)
    broker.find_task.return_value = None

    with pytest.raises(TaskNotRegisteredError):
        await TaskiqDispatcher(broker).enqueue("ingest.pipeline", document_id="d1")


# --- service: the row is already durable, so keep the 202 ---------------


async def test_create_still_succeeds_when_the_queue_is_unreachable() -> None:
    broker = AsyncMock()
    broker.enqueue.side_effect = TaskDispatchUnavailable("broker unreachable")
    service, repo = _service(broker)

    with capture_logs() as logs:
        document_id = await service.create(request=_inline(), create_user="alice")

    assert document_id  # 202, not a 500
    repo.create.assert_awaited_once()  # the row is durable
    assert any(log["event"] == "ingest.dispatch_deferred" for log in logs)


async def test_create_still_propagates_an_unregistered_task() -> None:
    broker = AsyncMock()
    broker.enqueue.side_effect = TaskNotRegisteredError("ingest.pipeline")
    service, _ = _service(broker)

    with pytest.raises(TaskNotRegisteredError):
        await service.create(request=_inline(), create_user="alice")


async def test_rerun_still_succeeds_when_the_queue_is_unreachable() -> None:
    broker = AsyncMock()
    broker.enqueue.side_effect = TaskDispatchUnavailable("broker unreachable")
    service, repo = _service(broker)
    repo.mark_for_rerun.return_value = "ok"

    await service.rerun("d1")  # must not raise
