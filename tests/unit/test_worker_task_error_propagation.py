"""Verify ingest_supersede_task and backfill_candidate_task re-raise exceptions.

Without top-level exception handling, failures in these tasks are silently
dropped by TaskIQ's internal handler — operators see nothing in logs.
With the fix, the exception is logged (structured) and re-raised so TaskIQ
marks the task as failed and the error surfaces in monitoring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# ingest_supersede_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_task_reraises_on_failure() -> None:
    """ingest_supersede_task must re-raise exceptions so TaskIQ marks the task failed.

    Previously the task had no try/except, so TaskIQ would silently log at
    framework level with no operator-visible structured log entry.
    """
    container = MagicMock()
    container.doc_repo = MagicMock()
    container.minio_registry = MagicMock()
    container.registry = MagicMock()

    from ragent.workers import ingest as worker_mod

    boom = RuntimeError("DB connection lost")

    with (
        patch("ragent.bootstrap.composition.get_container", return_value=container),
        patch("ragent.services.ingest_service.IngestService") as MockSvc,
        pytest.raises(RuntimeError, match="DB connection lost"),
    ):
        MockSvc.return_value.supersede = AsyncMock(side_effect=boom)
        await worker_mod.ingest_supersede_task("s-id", "src-1", "confluence")


@pytest.mark.asyncio
async def test_supersede_task_succeeds_normally() -> None:
    """Happy path: task completes without raising."""
    container = MagicMock()
    container.doc_repo = MagicMock()
    container.minio_registry = MagicMock()
    container.registry = MagicMock()

    from ragent.workers import ingest as worker_mod

    with (
        patch("ragent.bootstrap.composition.get_container", return_value=container),
        patch("ragent.services.ingest_service.IngestService") as MockSvc,
    ):
        MockSvc.return_value.supersede = AsyncMock(return_value=None)
        # Should not raise
        await worker_mod.ingest_supersede_task("s-id", "src-1", "confluence")


# ---------------------------------------------------------------------------
# backfill_candidate_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_task_reraises_on_failure() -> None:
    """backfill_candidate_task must re-raise exceptions (same contract as supersede)."""
    from ragent.workers import backfill as worker_mod

    container = MagicMock()
    container.es_client = MagicMock()
    container.embedding_registry = MagicMock()
    container.embedding_registry.refresh = AsyncMock(side_effect=ConnectionError("ES down"))

    with (
        patch("ragent.bootstrap.composition.get_container", return_value=container),
        pytest.raises(ConnectionError, match="ES down"),
    ):
        await worker_mod.backfill_candidate_task("chunks_v1", "chunks_v2")


@pytest.mark.asyncio
async def test_backfill_task_succeeds_normally() -> None:
    """Happy path: task returns without raising when registry has < 2 write models."""
    from ragent.workers import backfill as worker_mod

    container = MagicMock()
    container.es_client = MagicMock()
    container.embedding_registry = MagicMock()
    container.embedding_registry.refresh = AsyncMock(return_value=None)
    container.embedding_registry.write_models.return_value = iter([MagicMock()])  # only 1 model

    with patch("ragent.bootstrap.composition.get_container", return_value=container):
        # Should not raise — skips backfill due to not-in-candidate-state
        await worker_mod.backfill_candidate_task("chunks_v1", "chunks_v2")
