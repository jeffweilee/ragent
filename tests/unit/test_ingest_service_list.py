"""T2.11 — IngestService.list: cursor pagination, limit clamp (S15, B28)."""

import datetime
from unittest.mock import AsyncMock, MagicMock

from ragent.repositories.document_repository import DocumentRow
from ragent.services.ingest_service import IngestService


def _dt():
    return datetime.datetime.now(datetime.UTC)


def _make_doc(doc_id: str, **kwargs) -> DocumentRow:
    base = dict(
        document_id=doc_id,
        create_user="alice",
        source_id="S",
        source_app="app",
        source_title="T",
        source_meta=None,
        object_key="key",
        status="READY",
        attempt=1,
        created_at=_dt(),
        updated_at=_dt(),
    )
    base.update(kwargs)
    return DocumentRow(**base)


def _make_service(rows):
    repo = AsyncMock()
    repo.list.return_value = rows
    svc = IngestService(repo=repo, storage=MagicMock(), broker=MagicMock(), registry=MagicMock())
    return svc, repo


async def test_list_returns_items():
    rows = [_make_doc(f"ID{i:03}") for i in range(3)]
    svc, _ = _make_service(rows)
    result = await svc.list()
    assert len(result.items) == 3
    assert result.next_cursor is None


async def test_list_next_cursor_when_more_items():
    """When backend returns limit+1 rows, next_cursor is last item's document_id."""
    rows = [_make_doc(f"ID{i:03}") for i in range(6)]  # 6 rows for limit=5
    svc, repo = _make_service(rows)
    result = await svc.list(limit=5)
    assert len(result.items) == 5
    assert result.next_cursor == "ID004"


async def test_list_no_cursor_when_exact_limit():
    """Exactly limit rows → no next_cursor."""
    rows = [_make_doc(f"ID{i:03}") for i in range(5)]
    svc, _ = _make_service(rows)
    result = await svc.list(limit=5)
    assert result.next_cursor is None


async def test_list_clamps_limit_to_max():
    """limit=200 clamped to INGEST_LIST_MAX_LIMIT (100)."""
    rows = []
    svc, repo = _make_service(rows)
    await svc.list(limit=200)
    call_kwargs = repo.list.call_args[1]
    assert call_kwargs["limit"] <= 101  # max+1 for look-ahead


async def test_list_passes_after_cursor():
    rows = [_make_doc("ID005")]
    svc, repo = _make_service(rows)
    await svc.list(after="ID004", limit=10)
    call_kwargs = repo.list.call_args[1]
    assert call_kwargs["after"] == "ID004"


async def test_list_no_cursor_on_empty():
    svc, _ = _make_service([])
    result = await svc.list()
    assert result.items == []
    assert result.next_cursor is None
