"""Lifespan infra readiness/close — ES, DB, TaskIQ broker (B27, journal 2026-05-06)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def fake_container() -> SimpleNamespace:
    es_client = MagicMock()
    es_client.cluster.health.return_value = {"status": "green"}
    es_client.indices.exists.return_value = True
    engine = MagicMock()
    engine.dispose = AsyncMock()

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return None

    engine.connect = MagicMock(return_value=_Conn())
    return SimpleNamespace(es_client=es_client, engine=engine, token_managers=())


@pytest.fixture
def fake_broker() -> MagicMock:
    broker = MagicMock()
    broker.find_task.return_value = MagicMock()  # any non-None task object
    return broker


@pytest.mark.asyncio
async def test_check_infra_ready_passes_when_all_ok(fake_container, fake_broker) -> None:
    from ragent.bootstrap.app import _check_infra_ready

    await _check_infra_ready(fake_container, fake_broker)


@pytest.mark.asyncio
async def test_check_infra_ready_raises_when_broker_task_missing(
    fake_container, fake_broker
) -> None:
    from ragent.bootstrap.app import _check_infra_ready

    fake_broker.find_task.side_effect = lambda label: (
        None if label == "ingest.pipeline" else MagicMock()
    )

    with pytest.raises(RuntimeError, match="ingest.pipeline"):
        await _check_infra_ready(fake_container, fake_broker)


@pytest.mark.asyncio
async def test_check_infra_ready_raises_when_db_probe_fails(fake_container, fake_broker) -> None:
    from ragent.bootstrap.app import _check_infra_ready

    fake_container.engine.connect.side_effect = RuntimeError("db unreachable")

    with pytest.raises(RuntimeError, match="mariadb"):
        await _check_infra_ready(fake_container, fake_broker)


@pytest.mark.asyncio
async def test_check_infra_ready_raises_when_es_unhealthy(fake_container, fake_broker) -> None:
    from ragent.bootstrap.app import _check_infra_ready

    fake_container.es_client.cluster.health.return_value = {"status": "red"}

    with pytest.raises(RuntimeError, match="es"):
        await _check_infra_ready(fake_container, fake_broker)


@pytest.mark.asyncio
async def test_close_infra_closes_es_and_disposes_engine(fake_container) -> None:
    from ragent.bootstrap.app import _close_infra

    await _close_infra(fake_container)

    fake_container.es_client.close.assert_called_once()
    fake_container.engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_infra_continues_when_es_close_raises(fake_container) -> None:
    """Shutdown is best-effort — one failing close must not block others."""
    from ragent.bootstrap.app import _close_infra

    fake_container.es_client.close.side_effect = RuntimeError("boom")

    await _close_infra(fake_container)  # should not raise

    fake_container.engine.dispose.assert_awaited_once()
