"""TaskiqDispatcher / BlockingTaskiqDispatcher contract (B25, B27).

The producer side of TaskIQ requires `await broker.startup()` and
`await registered_task.kiq(**kwargs)`. These dispatchers wrap that
call sequence behind a stable `enqueue(label, **kwargs)` surface so
service / reconciler code stays decoupled from the broker brand.
"""

from __future__ import annotations

import asyncio

import pytest
from taskiq.brokers.inmemory_broker import InMemoryBroker

from ragent.bootstrap.dispatcher import (
    BlockingTaskiqDispatcher,
    TaskiqDispatcher,
    TaskNotRegisteredError,
)


@pytest.fixture
def broker_with_task() -> tuple[InMemoryBroker, list[tuple]]:
    broker = InMemoryBroker()
    seen: list[tuple] = []

    @broker.task("ingest.pipeline")
    async def _pipeline(document_id: str) -> None:
        seen.append((document_id,))

    return broker, seen


def test_async_dispatcher_enqueues_registered_task(broker_with_task) -> None:
    broker, seen = broker_with_task
    dispatcher = TaskiqDispatcher(broker)

    async def _go() -> None:
        await broker.startup()
        try:
            await dispatcher.enqueue("ingest.pipeline", document_id="DOC1")
        finally:
            await broker.shutdown()

    asyncio.run(_go())
    assert seen == [("DOC1",)]


def test_async_dispatcher_raises_for_unregistered_label(broker_with_task) -> None:
    broker, _ = broker_with_task
    dispatcher = TaskiqDispatcher(broker)

    async def _go() -> None:
        await broker.startup()
        try:
            with pytest.raises(TaskNotRegisteredError, match="not registered"):
                await dispatcher.enqueue("ingest.unknown", document_id="X")
        finally:
            await broker.shutdown()

    asyncio.run(_go())


def test_blocking_dispatcher_runs_in_threadpool_context(broker_with_task) -> None:
    """BlockingTaskiqDispatcher.enqueue() must be callable from a worker
    thread while an asyncio loop runs on the parent thread — the topology
    FastAPI's `run_in_threadpool` (= `anyio.to_thread.run_sync`) creates."""
    import functools

    import anyio

    broker, seen = broker_with_task
    dispatcher = BlockingTaskiqDispatcher(TaskiqDispatcher(broker))

    async def _go() -> None:
        await broker.startup()
        try:
            await anyio.to_thread.run_sync(
                functools.partial(dispatcher.enqueue, "ingest.pipeline", document_id="DOC2")
            )
        finally:
            await broker.shutdown()

    asyncio.run(_go())
    assert seen == [("DOC2",)]
