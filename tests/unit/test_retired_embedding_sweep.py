"""T-EM.18 / T-EM-R.8 — Reconciler arm: retired-embedding-index sweep.

After /commit or /abort adds an entry to embedding.retired (with index_name),
this arm:

1. Reads embedding.retired from system_settings.
2. For each entry with cleanup_done=false and index_name set, calls
   DELETE /index_name on ES (idempotent — 404 treated as success).
3. Marks the entry cleanup_done=true via an optimistic-locked transition.

Entries without index_name (legacy field-based entries) are skipped.
Errors on a single entry do not poison the sweep for remaining entries.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _entry(index_name: str, cleanup_done: bool = False) -> dict:
    return {
        "name": "bge-m3",
        "dim": 1024,
        "index_name": index_name,
        "retired_at": "2026-05-15T12:00:00Z",
        "cleanup_done": cleanup_done,
    }


def _legacy_entry(field: str, cleanup_done: bool = False) -> dict:
    """Pre-T-EM-R.8 retired entry that uses 'field' instead of 'index_name'."""
    return {
        "name": field.split("_")[1],
        "dim": int(field.rsplit("_", 1)[-1]),
        "field": field,
        "retired_at": "2026-05-15T12:00:00Z",
        "cleanup_done": cleanup_done,
    }


def _reconciler(
    *,
    settings_repo: AsyncMock | None = None,
    es_client: AsyncMock | None = None,
    chunks_index: str = "chunks_v1",
):
    from ragent.reconciler import Reconciler

    repo = MagicMock()
    broker = MagicMock()
    return Reconciler(
        repo=repo,
        broker=broker,
        registry=None,
        settings_repo=settings_repo,
        es_client=es_client,
        chunks_index=chunks_index,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_sweep_deletes_retired_index() -> None:
    """T-EM-R.8 — sweep calls DELETE /index_name, not update_by_query."""
    settings = AsyncMock()
    settings.get.return_value = [_entry("chunks_v1", cleanup_done=False)]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    es.indices.delete.assert_awaited_once()
    kwargs = es.indices.delete.call_args.kwargs
    assert kwargs["index"] == "chunks_v1"
    es.update_by_query.assert_not_awaited()


async def test_sweep_marks_entry_cleanup_done_after_delete() -> None:
    import copy

    settings = AsyncMock()
    live = [_entry("chunks_v1", cleanup_done=False)]
    pre_mutation_snapshot = copy.deepcopy(live)
    settings.get.return_value = live
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    settings.transition.assert_awaited_once()
    args, kwargs = settings.transition.call_args
    updates = args[0] if args else kwargs.get("updates", {})
    written = updates["embedding.retired"]
    assert written[0]["cleanup_done"] is True
    expect = kwargs.get("expect")
    assert expect == {"embedding.retired": pre_mutation_snapshot}


async def test_sweep_treats_404_as_success() -> None:
    """Index already deleted (404) → cleanup_done=True, not an error."""
    from elasticsearch import NotFoundError

    settings = AsyncMock()
    settings.get.return_value = [_entry("chunks_v1", cleanup_done=False)]
    es = AsyncMock()
    es.indices.delete.side_effect = NotFoundError(
        message="index_not_found_exception",
        meta=MagicMock(status=404),
        body={"error": "index_not_found_exception"},
    )

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    settings.transition.assert_awaited_once()
    args = settings.transition.call_args.args
    written = args[0]["embedding.retired"]
    assert written[0]["cleanup_done"] is True


# ---------------------------------------------------------------------------
# Legacy entries without index_name are skipped
# ---------------------------------------------------------------------------


async def test_sweep_skips_entries_without_index_name() -> None:
    """Legacy field-based entries have no index to delete — skip silently."""
    settings = AsyncMock()
    settings.get.return_value = [_legacy_entry("embedding_bgem3v2_768", cleanup_done=False)]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    es.indices.delete.assert_not_awaited()
    settings.transition.assert_not_awaited()


# ---------------------------------------------------------------------------
# Already-cleaned entries are skipped
# ---------------------------------------------------------------------------


async def test_sweep_skips_entries_already_cleanup_done() -> None:
    settings = AsyncMock()
    settings.get.return_value = [_entry("chunks_v1", cleanup_done=True)]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    es.indices.delete.assert_not_awaited()
    settings.transition.assert_not_awaited()


async def test_sweep_processes_only_pending_entries_in_mixed_list() -> None:
    settings = AsyncMock()
    settings.get.return_value = [
        _entry("chunks_v1", cleanup_done=True),
        _entry("chunks_v2", cleanup_done=False),
        _entry("chunks_v3", cleanup_done=True),
    ]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    assert es.indices.delete.await_count == 1
    kwargs = es.indices.delete.call_args.kwargs
    assert kwargs["index"] == "chunks_v2"


# ---------------------------------------------------------------------------
# Empty list / no-op paths
# ---------------------------------------------------------------------------


async def test_sweep_noop_when_retired_list_empty() -> None:
    settings = AsyncMock()
    settings.get.return_value = []
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    es.indices.delete.assert_not_awaited()
    settings.transition.assert_not_awaited()


async def test_sweep_noop_when_settings_repo_not_wired() -> None:
    rec = _reconciler(settings_repo=None, es_client=AsyncMock())
    await rec._sweep_retired_embedding_indices()


async def test_sweep_noop_when_es_client_not_wired() -> None:
    rec = _reconciler(settings_repo=AsyncMock(), es_client=None)
    await rec._sweep_retired_embedding_indices()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_sweep_logs_and_continues_when_single_entry_fails() -> None:
    settings = AsyncMock()
    settings.get.return_value = [
        _entry("chunks_v1", cleanup_done=False),
        _entry("chunks_v2", cleanup_done=False),
    ]
    es = AsyncMock()
    es.indices.delete.side_effect = [RuntimeError("ES blip"), None]

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_indices()

    assert es.indices.delete.await_count == 2
    settings.transition.assert_awaited_once()
    args = settings.transition.call_args.args
    written = args[0]["embedding.retired"]
    by_index = {e["index_name"]: e for e in written}
    assert by_index["chunks_v1"]["cleanup_done"] is False
    assert by_index["chunks_v2"]["cleanup_done"] is True


# ---------------------------------------------------------------------------
# Integration with the existing tick
# ---------------------------------------------------------------------------


async def test_run_async_invokes_retired_index_sweep_when_wired() -> None:
    settings = AsyncMock()
    settings.get.return_value = []
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    import inspect

    source = inspect.getsource(type(rec)._run_async)
    assert "_sweep_retired_embedding_indices" in source


def test_per_tick_runner_wires_settings_repo_and_es_client() -> None:
    """_PerTickRunner._tick() must pass settings_repo and es_client to Reconciler
    so _sweep_retired_embedding_indices is active in production, not silently skipped."""
    import inspect

    from ragent.reconciler import _PerTickRunner

    source = inspect.getsource(_PerTickRunner._tick)
    assert "settings_repo=container.system_settings_repo" in source
    assert "es_client=container.es_client" in source


def test_per_tick_runner_refreshes_embedding_registry() -> None:
    """_PerTickRunner._tick() must call container.embedding_registry.refresh() before
    fan_out_delete so VectorExtractor._delete_indices() sees a warm cache and cleans
    both stable and candidate indices during CANDIDATE/CUTOVER lifecycle (B62)."""
    import inspect

    from ragent.reconciler import _PerTickRunner

    source = inspect.getsource(_PerTickRunner._tick)
    assert "container.embedding_registry.refresh()" in source, (
        "_PerTickRunner._tick() must await container.embedding_registry.refresh() "
        "so VectorExtractor.delete() fans out to candidate_index during lifecycle migration."
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
