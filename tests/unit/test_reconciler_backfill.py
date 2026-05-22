"""T-EM-R.9 — Reconciler backfill arm: auto-enqueue when candidate under-covered."""

from unittest.mock import AsyncMock, MagicMock


def _reconciler(*, settings_repo=None, es_client=None, broker=None, chunks_index="chunks_v1"):
    from ragent.reconciler import Reconciler

    return Reconciler(
        repo=MagicMock(),
        broker=broker or AsyncMock(),
        registry=None,
        settings_repo=settings_repo,
        es_client=es_client,
        chunks_index=chunks_index,
    )


async def test_backfill_enqueued_when_candidate_under_covered() -> None:
    """CANDIDATE state + coverage < 0.99 → broker.enqueue called with index names."""
    settings = AsyncMock()
    settings.get.return_value = {"name": "bge-m3-v2", "dim": 768, "index_name": "chunks_v2"}
    es = AsyncMock()
    es.count.side_effect = [{"count": 100}, {"count": 50}]
    broker = AsyncMock()

    rec = _reconciler(
        settings_repo=settings, es_client=es, broker=broker, chunks_index="chunks_v1"
    )
    await rec._backfill_candidate_embeddings()

    broker.enqueue.assert_awaited_once_with(
        "ingest.backfill_candidate",
        stable_index="chunks_v1",
        candidate_index="chunks_v2",
    )


async def test_backfill_not_enqueued_when_idle_state() -> None:
    """IDLE state (no candidate) → no enqueue, no ES count calls."""
    settings = AsyncMock()
    settings.get.return_value = None
    es = AsyncMock()
    broker = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es, broker=broker)
    await rec._backfill_candidate_embeddings()

    broker.enqueue.assert_not_awaited()
    es.count.assert_not_awaited()


async def test_backfill_not_enqueued_when_coverage_sufficient() -> None:
    """Coverage ≥ 0.99 → no enqueue (candidate already fully backfilled)."""
    settings = AsyncMock()
    settings.get.return_value = {"name": "bge-m3-v2", "dim": 768, "index_name": "chunks_v2"}
    es = AsyncMock()
    es.count.side_effect = [{"count": 100}, {"count": 99}]
    broker = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es, broker=broker)
    await rec._backfill_candidate_embeddings()

    broker.enqueue.assert_not_awaited()
