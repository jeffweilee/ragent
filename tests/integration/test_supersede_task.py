"""T3.2c — Supersede task: pops oldest loser per tx, idempotent re-run (P-C, S17-S22)."""

import datetime
from unittest.mock import MagicMock

import pytest

from ragent.repositories.document_repository import DocumentRow

pytestmark = pytest.mark.docker


def _dt(offset_sec: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(
        seconds=offset_sec
    )


def _make_doc(
    doc_id: str,
    source_id: str = "S1",
    source_app: str = "confluence",
    created_offset: int = 0,
) -> DocumentRow:
    return DocumentRow(
        document_id=doc_id,
        create_user="alice",
        source_id=source_id,
        source_app=source_app,
        source_title="T",
        source_workspace=None,
        object_key=f"{source_app}_{source_id}_{doc_id}",
        status="READY",
        attempt=1,
        created_at=_dt(created_offset),
        updated_at=_dt(created_offset),
    )


def _make_service(side_effects: list) -> tuple:
    """Build IngestService with repo that returns losers from side_effects list then None."""
    from ragent.services.ingest_service import IngestService

    repo = MagicMock()
    storage = MagicMock()
    broker = MagicMock()

    # pop_oldest_loser returns each doc in order, then None (convergence)
    repo.pop_oldest_loser_for_supersede.side_effect = side_effects + [None]
    repo.acquire_nowait.side_effect = lambda doc_id: _make_doc(doc_id)

    svc = IngestService(repo=repo, storage=storage, broker=broker)
    return svc, repo, storage, broker


def test_supersede_pops_and_deletes_oldest_loser():
    """Single loser is cascade-deleted, survivor remains (S17)."""
    loser = _make_doc("DOC001", created_offset=0)
    svc, repo, storage, broker = _make_service([loser])
    svc.supersede(survivor_id="DOC002", source_id="S1", source_app="confluence")
    repo.delete.assert_called_once_with("DOC001")


def test_supersede_deletes_multiple_losers_one_at_a_time():
    """Two losers deleted in separate iterations — single-loser-per-tx (P-C, S17)."""
    loser1 = _make_doc("DOC001", created_offset=0)
    loser2 = _make_doc("DOC002", created_offset=1)
    svc, repo, storage, broker = _make_service([loser1, loser2])
    svc.supersede(survivor_id="DOC003", source_id="S1", source_app="confluence")
    assert repo.pop_oldest_loser_for_supersede.call_count == 3  # 2 losers + 1 None
    assert repo.delete.call_count == 2


def test_supersede_idempotent_when_only_survivor_remains():
    """No losers → no deletes; safe re-run (S19)."""
    svc, repo, storage, broker = _make_service([])
    svc.supersede(survivor_id="DOC001", source_id="S1", source_app="confluence")
    repo.delete.assert_not_called()


def test_supersede_different_source_app_coexists():
    """Different source_app with same source_id is a different identity — untouched (S22)."""
    # pop returns None immediately (no loser for this (source_id, source_app) pair)
    svc, repo, storage, broker = _make_service([])
    svc.supersede(survivor_id="DOC_SLACK", source_id="S1", source_app="slack")
    repo.delete.assert_not_called()


def test_supersede_calls_delete_chunks_for_each_loser():
    loser = _make_doc("DOC001", created_offset=0)
    svc, repo, storage, broker = _make_service([loser])
    svc.supersede(survivor_id="DOC002", source_id="S1", source_app="confluence")
    repo.delete_chunks.assert_called_once_with("DOC001")
