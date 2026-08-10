"""IngestService.batch_rerun — batch retry for UPLOADED/PENDING/FAILED docs."""

import datetime
from unittest.mock import AsyncMock, MagicMock

from ragent.bootstrap.dispatcher import TaskiqDispatcher
from ragent.extractors.registry import PluginRegistry
from ragent.repositories.document_repository import DocumentRepository, DocumentRow
from ragent.services.ingest_service import IngestService
from ragent.storage.minio_registry import MinioSiteRegistry


def _dt(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)


def _make_service():
    repo = AsyncMock(spec=DocumentRepository)
    broker = AsyncMock(spec=TaskiqDispatcher)
    svc = IngestService(
        repo=repo,
        storage=MagicMock(spec=MinioSiteRegistry),
        broker=broker,
        registry=MagicMock(spec=PluginRegistry),
    )
    return svc, repo, broker


def _doc(document_id: str = "DOC1", status: str = "FAILED") -> DocumentRow:
    return DocumentRow(
        document_id=document_id,
        create_user="alice",
        source_id="s1",
        source_app="app1",
        source_title="T",
        source_meta=None,
        object_key="key",
        status=status,
        attempt=1,
        created_at=_dt("2026-01-01T00:00:00"),
        updated_at=_dt("2026-01-01T00:00:00"),
    )


# ---------------------------------------------------------------------------
# dry_run behaviour
# ---------------------------------------------------------------------------


async def test_dry_run_returns_before_counts_twice():
    svc, repo, broker = _make_service()
    before_counts = {"FAILED": 3}
    repo.count_by_statuses.return_value = before_counts

    before, after, queued, skipped, _deferred = await svc.batch_rerun(
        statuses=["FAILED"], dry_run=True
    )

    assert before == before_counts
    assert after == before_counts
    assert queued == 0
    assert skipped == 0
    repo.mark_for_rerun.assert_not_called()
    broker.enqueue.assert_not_called()


async def test_dry_run_does_not_call_list_by_statuses():
    svc, repo, broker = _make_service()
    repo.count_by_statuses.return_value = {}

    await svc.batch_rerun(statuses=["FAILED"], dry_run=True)

    repo.list_by_statuses.assert_not_called()


# ---------------------------------------------------------------------------
# execute (dry_run=False)
# ---------------------------------------------------------------------------


async def test_all_ok_queues_all():
    svc, repo, broker = _make_service()
    docs = [_doc("D1"), _doc("D2"), _doc("D3")]
    repo.list_by_statuses.return_value = docs
    repo.mark_for_rerun.return_value = "ok"
    repo.count_by_statuses.side_effect = [{"FAILED": 3}, {"FAILED": 0}]

    before, after, queued, skipped, _deferred = await svc.batch_rerun(statuses=["FAILED"])

    assert queued == 3
    assert skipped == 0
    assert broker.enqueue.await_count == 3


async def test_skips_not_rerunnable():
    svc, repo, broker = _make_service()
    docs = [_doc("D1"), _doc("D2")]
    repo.list_by_statuses.return_value = docs
    repo.mark_for_rerun.side_effect = ["ok", "not_rerunnable"]
    repo.count_by_statuses.side_effect = [{"FAILED": 2}, {"FAILED": 1}]

    before, after, queued, skipped, _deferred = await svc.batch_rerun(statuses=["FAILED"])

    assert queued == 1
    assert skipped == 1


async def test_skips_not_found():
    svc, repo, broker = _make_service()
    docs = [_doc("D1"), _doc("D2")]
    repo.list_by_statuses.return_value = docs
    repo.mark_for_rerun.side_effect = ["not_found", "ok"]
    repo.count_by_statuses.side_effect = [{"FAILED": 2}, {"FAILED": 1}]

    before, after, queued, skipped, _deferred = await svc.batch_rerun(statuses=["FAILED"])

    assert queued == 1
    assert skipped == 1


async def test_empty_list_returns_zeros():
    svc, repo, broker = _make_service()
    repo.list_by_statuses.return_value = []
    before_counts = {"FAILED": 0}
    repo.count_by_statuses.return_value = before_counts

    before, after, queued, skipped, _deferred = await svc.batch_rerun(statuses=["FAILED"])

    assert queued == 0
    assert skipped == 0
    assert before == before_counts
    assert after == before_counts


async def test_mark_before_enqueue_order():
    svc, repo, broker = _make_service()
    docs = [_doc("D1"), _doc("D2")]
    repo.list_by_statuses.return_value = docs
    order: list[str] = []
    repo.mark_for_rerun.side_effect = lambda doc_id: order.append(f"mark:{doc_id}") or "ok"
    broker.enqueue.side_effect = lambda task, *, document_id: order.append(f"enqueue:{document_id}")
    repo.count_by_statuses.side_effect = [{"FAILED": 2}, {"FAILED": 0}]

    await svc.batch_rerun(statuses=["FAILED"])

    assert order == ["mark:D1", "enqueue:D1", "mark:D2", "enqueue:D2"]


async def test_after_count_queried_after_mutations():
    svc, repo, broker = _make_service()
    docs = [_doc("D1")]
    repo.list_by_statuses.return_value = docs
    repo.mark_for_rerun.return_value = "ok"

    call_order: list[str] = []
    repo.count_by_statuses.side_effect = lambda *a, **k: (
        call_order.append("count") or ({"FAILED": 1} if len(call_order) == 1 else {"FAILED": 0})
    )
    repo.mark_for_rerun.side_effect = lambda *a, **k: call_order.append("mark") or "ok"

    before, after, _, _, _ = await svc.batch_rerun(statuses=["FAILED"])

    # count is called twice; mark happens between the two counts
    assert call_order.index("mark") > call_order.index("count")
    assert call_order.count("count") == 2


async def test_log_preview_emitted():
    import structlog

    svc, repo, broker = _make_service()
    repo.count_by_statuses.return_value = {"FAILED": 2}

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"], dry_run=True)

    assert any(e["event"] == "ingest.batch_rerun_preview" for e in logs)


async def test_log_dispatched_emitted():
    import structlog

    svc, repo, broker = _make_service()
    repo.list_by_statuses.return_value = [_doc("D1")]
    repo.mark_for_rerun.return_value = "ok"
    repo.count_by_statuses.side_effect = [{"FAILED": 1}, {"FAILED": 0}]

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"])

    assert any(e["event"] == "ingest.batch_rerun_dispatched" for e in logs)


async def test_log_started_emitted_on_execute():
    import structlog

    svc, repo, broker = _make_service()
    repo.list_by_statuses.return_value = []
    repo.count_by_statuses.return_value = {}

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"])

    assert any(e["event"] == "ingest.batch_rerun_started" for e in logs)


async def test_log_started_emitted_on_dry_run():
    import structlog

    svc, repo, broker = _make_service()
    repo.count_by_statuses.return_value = {}

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"], dry_run=True)

    assert any(e["event"] == "ingest.batch_rerun_started" for e in logs)


async def test_log_started_before_db_call():
    import structlog

    svc, repo, broker = _make_service()
    order: list[str] = []
    repo.count_by_statuses.side_effect = lambda *a, **k: order.append("db") or {}
    repo.list_by_statuses.return_value = []

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"])

    started_idx = next(i for i, e in enumerate(logs) if e["event"] == "ingest.batch_rerun_started")
    assert started_idx == 0
    assert order[0] == "db"  # db called after started log emitted at index 0


async def test_log_per_item_dispatch_emitted():
    import structlog

    svc, repo, broker = _make_service()
    repo.list_by_statuses.return_value = [_doc("D1"), _doc("D2")]
    repo.mark_for_rerun.return_value = "ok"
    repo.count_by_statuses.side_effect = [{"FAILED": 2}, {"FAILED": 0}]

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"])

    item_logs = [e for e in logs if e["event"] == "ingest.batch_rerun_item_dispatched"]
    assert len(item_logs) == 2
    assert {e["document_id"] for e in item_logs} == {"D1", "D2"}


async def test_operator_id_in_started_log():
    import structlog

    svc, repo, broker = _make_service()
    repo.count_by_statuses.return_value = {}
    repo.list_by_statuses.return_value = []

    with structlog.testing.capture_logs() as logs:
        await svc.batch_rerun(statuses=["FAILED"], operator_id="ops-user")

    started = next(e for e in logs if e["event"] == "ingest.batch_rerun_started")
    assert started["operator_id"] == "ops-user"


# ---------------------------------------------------------------------------
# filter kwargs forwarding
# ---------------------------------------------------------------------------


async def test_filter_kwargs_forwarded_to_repo():
    svc, repo, broker = _make_service()
    repo.list_by_statuses.return_value = []
    repo.count_by_statuses.return_value = {}
    after_ts = _dt("2026-06-01T00:00:00")

    await svc.batch_rerun(
        statuses=["FAILED"],
        source_app="myapp",
        source_id="s1",
        created_after=after_ts,
        limit=50,
    )

    repo.count_by_statuses.assert_awaited_with(
        ["FAILED"], source_app="myapp", source_id="s1", created_after=after_ts
    )
    repo.list_by_statuses.assert_awaited_with(
        ["FAILED"],
        source_app="myapp",
        source_id="s1",
        created_after=after_ts,
        limit=50,
    )
