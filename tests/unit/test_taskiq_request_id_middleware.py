"""T-APL.8 — request_id / user_id propagation across TaskIQ enqueue ↔ execute."""

from __future__ import annotations

import pytest
import structlog
from taskiq import TaskiqMessage, TaskiqResult

from ragent.middleware.taskiq_context import (
    PROPAGATED_KEYS,
    StructlogContextMiddleware,
)


@pytest.fixture(autouse=True)
def _isolate_contextvars():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _msg(labels: dict | None = None) -> TaskiqMessage:
    return TaskiqMessage(
        task_id="t-1",
        task_name="ingest.pipeline",
        labels=labels or {},
        args=[],
        kwargs={},
    )


def test_pre_send_snapshots_contextvars_into_labels() -> None:
    mw = StructlogContextMiddleware()
    structlog.contextvars.bind_contextvars(request_id="req-abc", user_id="alice")
    out = mw.pre_send(_msg())
    assert out.labels["request_id"] == "req-abc"
    assert out.labels["user_id"] == "alice"


def test_pre_send_skips_keys_not_in_contextvars() -> None:
    mw = StructlogContextMiddleware()
    structlog.contextvars.bind_contextvars(request_id="req-only")
    out = mw.pre_send(_msg())
    assert out.labels == {"request_id": "req-only"}
    assert "user_id" not in out.labels


def test_pre_send_leaves_unrelated_contextvars_alone() -> None:
    mw = StructlogContextMiddleware()
    structlog.contextvars.bind_contextvars(document_id="DOC-1", custom_field="x")
    out = mw.pre_send(_msg())
    assert "document_id" not in out.labels
    assert "custom_field" not in out.labels


def test_pre_execute_rebinds_labels_into_contextvars() -> None:
    mw = StructlogContextMiddleware()
    mw.pre_execute(_msg({"request_id": "req-xyz", "user_id": "bob"}))
    ctx = structlog.contextvars.get_contextvars()
    assert ctx["request_id"] == "req-xyz"
    assert ctx["user_id"] == "bob"


def test_post_execute_unbinds_only_keys_present_in_labels() -> None:
    """A request_id NOT carried by the message must survive post_execute.

    Defence against erasing a contextvar the middleware never bound — e.g. a
    task body that re-bound `request_id` for its own scope.
    """
    mw = StructlogContextMiddleware()
    structlog.contextvars.bind_contextvars(request_id="set-by-task-body")
    # Message carries NO labels — middleware did not snapshot, must not unbind.
    msg = _msg()
    mw.pre_execute(msg)
    result: TaskiqResult[None] = TaskiqResult(is_err=False, return_value=None, execution_time=0.0)
    mw.post_execute(msg, result)
    assert structlog.contextvars.get_contextvars().get("request_id") == "set-by-task-body"


def test_post_execute_unbinds_propagated_keys() -> None:
    """When message carried labels, post_execute MUST clear them.

    Avoids leaking ids from one task into the next on the same worker coroutine.
    """
    mw = StructlogContextMiddleware()
    msg = _msg({"request_id": "req-1", "user_id": "u-1"})
    mw.pre_execute(msg)
    result: TaskiqResult[None] = TaskiqResult(is_err=False, return_value=None, execution_time=0.0)
    mw.post_execute(msg, result)
    ctx = structlog.contextvars.get_contextvars()
    for key in PROPAGATED_KEYS:
        assert key not in ctx, f"{key} leaked across task boundary"


async def test_request_id_propagates_end_to_end_via_inmemory_broker() -> None:
    """Real broker round-trip: producer pre_send → consumer pre_execute.

    Mock-based unit tests can hide TaskIQ contract drift (journal 2026-05-06).
    Uses InMemoryBroker so the middleware sees a real ``kiq()``/``listen()``
    cycle without Redis.
    """
    from taskiq import InMemoryBroker

    captured: dict = {}
    broker = InMemoryBroker()
    broker.add_middlewares(StructlogContextMiddleware())

    @broker.task("test.echo_request_id")
    async def echo() -> None:
        captured.update(structlog.contextvars.get_contextvars())

    await broker.startup()
    try:
        structlog.contextvars.bind_contextvars(request_id="req-e2e", user_id="alice")
        task = await echo.kiq()
        await task.wait_result()
    finally:
        await broker.shutdown()

    assert captured.get("request_id") == "req-e2e"
    assert captured.get("user_id") == "alice"


def test_pre_send_coerces_non_string_values() -> None:
    """Labels are serialised to the broker; non-str values must round-trip safely."""
    mw = StructlogContextMiddleware()
    structlog.contextvars.bind_contextvars(request_id=12345)
    out = mw.pre_send(_msg())
    assert out.labels["request_id"] == "12345"
