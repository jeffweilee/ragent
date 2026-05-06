"""TaskIQ producer-side dispatcher (B25, B27).

`AsyncBroker` exposes producer semantics through `await task.kiq(**kwargs)`
on the decorated task object — there is no `broker.enqueue(label, ...)`.
This module provides two thin wrappers so service / reconciler code can
keep a stable `enqueue(label, **kwargs)` seam:

- `TaskiqDispatcher` — async, used from already-async call sites
  (Reconciler).
- `BlockingTaskiqDispatcher` — sync wrapper; safe to call from a FastAPI
  worker thread (`run_in_threadpool`) because `anyio.from_thread.run`
  bridges back to the parent event loop.

Production wiring (api / reconciler) constructs these and injects them
into `IngestService` / `Reconciler` instead of the raw broker.
"""

from __future__ import annotations

from typing import Any

from anyio.from_thread import run as _run_from_thread
from taskiq import AsyncBroker


class TaskiqDispatcher:
    """Async-native dispatcher: `await dispatcher.enqueue(label, **kwargs)`."""

    def __init__(self, broker: AsyncBroker) -> None:
        self._broker = broker

    async def enqueue(self, label: str, **kwargs: Any) -> None:
        task = self._broker.find_task(label)
        if task is None:
            raise RuntimeError(f"taskiq task {label!r} is not registered")
        await task.kiq(**kwargs)


class BlockingTaskiqDispatcher:
    """Sync facade over `TaskiqDispatcher` for FastAPI threadpool callers.

    Must be invoked from a worker thread whose parent thread runs the
    asyncio event loop (the topology established by FastAPI's
    `run_in_threadpool` / `asyncio.to_thread`). Calling from the loop
    thread itself raises — that path should `await` the async dispatcher
    directly.
    """

    def __init__(self, async_dispatcher: TaskiqDispatcher) -> None:
        self._async = async_dispatcher

    def enqueue(self, label: str, **kwargs: Any) -> None:
        async def _call() -> None:
            await self._async.enqueue(label, **kwargs)

        _run_from_thread(_call)
