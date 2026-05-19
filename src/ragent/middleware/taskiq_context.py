"""T-APL.9 — propagate `request_id` / `user_id` across the TaskIQ enqueue/execute seam.

``RequestLoggingMiddleware`` binds ``request_id`` (and ``user_id`` when the
header is present) to ``structlog.contextvars`` for the duration of every
HTTP request. Without this middleware, those contextvars are NOT carried
across the broker boundary — worker logs (``ingest.step.*``, ``ingest.ready``,
``ingest.failed``) cannot correlate with the originating ``/ingest`` HTTP
request, breaking cross-process operator triage.

Producer side (``pre_send``): snapshot the relevant contextvars into the
task message's ``labels`` dict (which TaskIQ serialises to the broker payload).

Consumer side (``pre_execute``): rebind the labels into ``structlog.contextvars``
so every log emitted by the task body — including those from the existing
``wrap_pipeline_component`` wrapper — carries the inherited ids. ``post_execute``
unbinds them so the next task pulled by the same worker coroutine starts clean.
"""

from __future__ import annotations

from typing import Any

import structlog
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

PROPAGATED_KEYS: tuple[str, ...] = ("request_id", "user_id")


class StructlogContextMiddleware(TaskiqMiddleware):
    """Carry structlog contextvars across the TaskIQ producer/consumer seam."""

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        ctx = structlog.contextvars.get_contextvars()
        for key in PROPAGATED_KEYS:
            value = ctx.get(key)
            if value is not None:
                message.labels[key] = str(value)
        return message

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        bindings: dict[str, Any] = {
            key: message.labels[key] for key in PROPAGATED_KEYS if key in message.labels
        }
        if bindings:
            structlog.contextvars.bind_contextvars(**bindings)
        return message

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        # Symmetric with pre_execute: unbind only the keys this middleware
        # actually bound (i.e. those present in the message labels). A task
        # body that re-bound request_id for its own scope is left alone.
        bound = [key for key in PROPAGATED_KEYS if key in message.labels]
        if bound:
            structlog.contextvars.unbind_contextvars(*bound)
