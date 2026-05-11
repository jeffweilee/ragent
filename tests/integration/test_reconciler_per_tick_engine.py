"""T7.4.x(a) — Reconciler runner builds a fresh AsyncEngine per tick.

Regression: ``Reconciler.run()`` invokes ``asyncio.run()``, which closes the
event loop on exit. The SQLAlchemy ``AsyncEngine`` constructed inside
``_build_from_env()`` was bound to the first tick's loop, so the second
``run()`` call raised ``RuntimeError: Event loop is closed`` (see
``docs/00_plan.md::T7.4.x``).

This pins the engine-per-tick contract: ``_build_from_env()`` returns a
runner whose ``run()`` builds a fresh engine for every tick and disposes it
afterwards, so the chaos test can poll the reconciler in a long-running
loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def _stub_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.list_pending_stale.return_value = []
    repo.list_pending_exceeded.return_value = []
    repo.list_uploaded_stale.return_value = []
    repo.list_deleting_stale.return_value = []
    repo.find_multi_ready_groups.return_value = []
    return repo


def test_runner_builds_fresh_engine_each_tick(
    monkeypatch: pytest.MonkeyPatch, _stub_repo: AsyncMock
) -> None:
    # Pre-import workers before broker is patched so @broker.task decorates
    # ingest_pipeline_task with the real broker, not the MagicMock below.
    import ragent.workers.ingest  # noqa: F401

    monkeypatch.setenv("MARIADB_DSN", "mysql+aiomysql://x:y@h/db")

    import ragent.reconciler as rec_mod

    engines: list[MagicMock] = []

    def _fake_engine(_dsn: str) -> MagicMock:
        engine = MagicMock(name=f"engine{len(engines)}")
        engine.dispose = AsyncMock()
        engines.append(engine)
        return engine

    fake_broker = MagicMock()
    fake_broker.startup = AsyncMock()
    fake_broker.shutdown = AsyncMock()

    monkeypatch.setattr(rec_mod, "create_async_engine", _fake_engine)
    monkeypatch.setattr(rec_mod, "DocumentRepository", lambda engine: _stub_repo)
    monkeypatch.setattr("ragent.bootstrap.broker.broker", fake_broker)
    monkeypatch.setattr(
        "ragent.bootstrap.composition.get_container",
        lambda: MagicMock(registry=MagicMock()),
    )

    runner = rec_mod._build_from_env()
    runner.run()
    runner.run()

    assert len(engines) == 2, "engine must be rebuilt per tick"
    for engine in engines:
        assert engine.dispose.await_count == 1, "each per-tick engine must be disposed"
    assert fake_broker.startup.await_count == 2
    assert fake_broker.shutdown.await_count == 2
