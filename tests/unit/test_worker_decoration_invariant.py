"""Test-isolation invariant: ragent.workers.ingest is decorated with the
real broker at conftest import time.

Six unit tests previously failed when
``tests/integration/test_reconciler_per_tick_engine.py`` ran first
because that test monkeypatches ``ragent.bootstrap.broker.broker`` and
then triggers the first-time import of ``ragent.workers.ingest`` via
``_build_from_env()``. ``@broker.task("ingest.pipeline")`` then decorated
with the fake broker, replacing ``ingest_pipeline_task`` with a
``MagicMock``; ``monkeypatch`` restored the broker module attribute but
left the worker module permanently polluted.

This test asserts that, by the time pytest collects unit tests, the
worker module's two ``@broker.task`` decorations point at coroutine
functions on the real ``ListQueueBroker`` — not ``MagicMock`` proxies.
A future polluter that recreates the leak will fail this test loudly.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_ingest_pipeline_task_decorated_with_real_broker() -> None:
    from ragent.bootstrap.broker import broker as real_broker
    from ragent.workers import ingest as worker_mod

    task = worker_mod.ingest_pipeline_task
    assert not isinstance(task, MagicMock), (
        "ingest_pipeline_task is a MagicMock — the worker module was imported "
        "while the broker module attribute was monkeypatched. Pre-import in "
        "tests/conftest.py is missing or runs too late."
    )
    assert task.broker is real_broker, (
        f"ingest_pipeline_task.broker is {type(task.broker).__name__}, "
        "expected the real ListQueueBroker singleton."
    )


def test_ingest_supersede_task_decorated_with_real_broker() -> None:
    from ragent.bootstrap.broker import broker as real_broker
    from ragent.workers import ingest as worker_mod

    task = worker_mod.ingest_supersede_task
    assert not isinstance(task, MagicMock)
    assert task.broker is real_broker
