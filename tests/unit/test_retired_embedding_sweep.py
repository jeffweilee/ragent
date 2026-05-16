"""T-EM.18 — Reconciler arm: retired-embedding-field sweep (B50 §9).

ES does not let you drop a field from a mapping (only reindex can). After
``/embedding/v1/commit`` or ``/abort`` adds an entry to ``embedding.retired``,
this arm:

1. Reads ``embedding.retired`` from ``system_settings``.
2. For each entry with ``cleanup_done=false``, fires
   ``POST <chunks_index>/_update_by_query`` with a Painless script that
   removes the retired vector field from every doc that still has it.
3. Marks the entry ``cleanup_done=true`` via an optimistic-locked
   ``transition({embedding.retired: …})`` so concurrent ticks / admin
   actions don't clobber each other.

Errors on a single entry are logged and do not poison the sweep for
the remaining entries (next tick retries the failed one).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _entry(field: str, cleanup_done: bool = False) -> dict:
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
    """Build a Reconciler whose only-used dependencies are these three."""
    from ragent.reconciler import Reconciler

    repo = MagicMock()  # DocumentRepository — not used by this arm
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


async def test_sweep_clears_uncleaned_field_via_update_by_query() -> None:
    settings = AsyncMock()
    settings.get.return_value = [_entry("embedding_bgem3v2_768", cleanup_done=False)]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_fields()

    es.update_by_query.assert_awaited_once()
    kwargs = es.update_by_query.call_args.kwargs
    assert kwargs["index"] == "chunks_v1"
    body = kwargs["body"]
    assert "embedding_bgem3v2_768" in body["script"]["source"]
    assert body["query"]["exists"]["field"] == "embedding_bgem3v2_768"


async def test_sweep_marks_entry_cleanup_done_after_es_call() -> None:
    import copy

    settings = AsyncMock()
    live = [_entry("embedding_bgem3v2_768", cleanup_done=False)]
    pre_mutation_snapshot = copy.deepcopy(live)
    settings.get.return_value = live
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_fields()

    settings.transition.assert_awaited_once()
    args, kwargs = settings.transition.call_args
    updates = args[0] if args else kwargs.get("updates", {})
    written = updates["embedding.retired"]
    assert written[0]["cleanup_done"] is True
    # Optimistic-lock guard captures the PRE-sweep state so a concurrent
    # admin write (e.g. another retire entry appended) aborts the txn.
    expect = kwargs.get("expect")
    assert expect == {"embedding.retired": pre_mutation_snapshot}


# ---------------------------------------------------------------------------
# Already-cleaned entries are skipped
# ---------------------------------------------------------------------------


async def test_sweep_skips_entries_already_cleanup_done() -> None:
    settings = AsyncMock()
    settings.get.return_value = [_entry("embedding_bgem3_1024", cleanup_done=True)]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_fields()

    es.update_by_query.assert_not_awaited()
    settings.transition.assert_not_awaited()


async def test_sweep_processes_only_pending_entries_in_mixed_list() -> None:
    settings = AsyncMock()
    settings.get.return_value = [
        _entry("embedding_old1_1024", cleanup_done=True),
        _entry("embedding_old2_512", cleanup_done=False),
        _entry("embedding_old3_1024", cleanup_done=True),
    ]
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_fields()

    # Only the one pending entry was swept.
    assert es.update_by_query.await_count == 1
    body = es.update_by_query.call_args.kwargs["body"]
    assert "embedding_old2_512" in body["script"]["source"]


# ---------------------------------------------------------------------------
# Empty list / no-op paths
# ---------------------------------------------------------------------------


async def test_sweep_noop_when_retired_list_empty() -> None:
    settings = AsyncMock()
    settings.get.return_value = []
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_fields()

    es.update_by_query.assert_not_awaited()
    settings.transition.assert_not_awaited()


async def test_sweep_noop_when_settings_repo_not_wired() -> None:
    """Reconciler running pre-T-EM (no settings repo injected) must not
    crash — the arm just no-ops."""
    rec = _reconciler(settings_repo=None, es_client=AsyncMock())
    await rec._sweep_retired_embedding_fields()  # should not raise


async def test_sweep_noop_when_es_client_not_wired() -> None:
    rec = _reconciler(settings_repo=AsyncMock(), es_client=None)
    await rec._sweep_retired_embedding_fields()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_sweep_logs_and_continues_when_single_entry_fails() -> None:
    """A flaky ES update on entry 1 must not stop the sweep from trying
    entry 2 — they're independent. Entry 1 stays cleanup_done=false so the
    next tick retries it."""
    settings = AsyncMock()
    settings.get.return_value = [
        _entry("embedding_bad_768", cleanup_done=False),
        _entry("embedding_good_512", cleanup_done=False),
    ]
    es = AsyncMock()
    es.update_by_query.side_effect = [RuntimeError("ES blip"), {"updated": 100}]

    rec = _reconciler(settings_repo=settings, es_client=es)
    await rec._sweep_retired_embedding_fields()  # must not raise

    assert es.update_by_query.await_count == 2
    # Settings write happened: good marked done, bad stayed pending.
    settings.transition.assert_awaited_once()
    args = settings.transition.call_args.args
    written = args[0]["embedding.retired"]
    by_field = {e["field"]: e for e in written}
    assert by_field["embedding_bad_768"]["cleanup_done"] is False
    assert by_field["embedding_good_512"]["cleanup_done"] is True


# ---------------------------------------------------------------------------
# Integration with the existing tick (regression smoke)
# ---------------------------------------------------------------------------


async def test_run_async_invokes_retired_sweep_when_wired() -> None:
    """The new arm runs as part of the standard tick — it's not a separate
    cron job. Skipped silently when settings/es are not wired (pre-T-EM
    reconciler invocations stay unchanged)."""
    settings = AsyncMock()
    settings.get.return_value = []
    es = AsyncMock()

    rec = _reconciler(settings_repo=settings, es_client=es)
    # Don't run the whole tick (the other arms need a real repo); just
    # confirm the arm method exists and is wired into _run_async by name.
    import inspect

    source = inspect.getsource(type(rec)._run_async)
    assert "_sweep_retired_embedding_fields" in source


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
