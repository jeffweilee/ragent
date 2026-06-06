"""T-EM.11 — EmbeddingLifecycleService orchestrates the five admin actions.

Each public method:
1. Reads current state via the registry (caller is responsible for `refresh()`
   having succeeded).
2. Asserts the state-machine transition; raises `IllegalEmbeddingTransition`
   on rejection (mapped to 409 in router).
3. Performs side effects: ES mapping PUT (promote only), settings transition.
4. Returns a snapshot dict for the router to echo.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest


def _bgem3() -> dict:
    return {
        "name": "bge-m3",
        "dim": 1024,
        "api_url": "http://e1",
        "model_arg": "bge-m3",
        "field": "embedding_bgem3_1024",
    }


def _bgem3v2_with_promoted_at(secs_ago: int = 60) -> dict:
    return {
        "name": "bge-m3-v2",
        "dim": 768,
        "api_url": "http://e2",
        "model_arg": "bge-m3-v2",
        "field": "embedding_bgem3v2_768",
        "promoted_at": (datetime.now(timezone.utc) - timedelta(seconds=secs_ago)).isoformat(),
    }


class _FakeRegistry:
    """Minimal stand-in for ActiveModelRegistry exposing what the service needs."""

    def __init__(self, state, stable=None, candidate=None, retired=None):
        self._state = state
        self._stable_dict = stable or _bgem3()
        self._candidate_dict = candidate
        self._retired = retired or []

    def derived_state(self):
        return self._state

    @property
    def stable_dict(self):
        return self._stable_dict

    @property
    def candidate_dict(self):
        return self._candidate_dict

    @property
    def stable_raw(self):
        return self._stable_dict

    @property
    def candidate_raw(self):
        return self._candidate_dict

    @property
    def retired_list(self):
        return self._retired

    @property
    def stable_index(self) -> str:
        return self._stable_dict.get("index_name") or "chunks_v1"

    @property
    def candidate_index(self):
        if self._candidate_dict is None:
            return None
        return self._candidate_dict.get("index_name")

    @property
    def read_alias(self) -> str:
        return "chunks_v1_active"

    async def refresh(self, *, force: bool = False) -> None:
        """Service force-refreshes after every mutation; no-op for the fake."""
        return None


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


async def test_promote_from_idle_creates_new_index_and_writes_candidate() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.promote(
        name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2"
    )

    es.indices.create.assert_awaited_once()
    assert es.indices.create.call_args.kwargs["index"] == "chunks_v2"

    repo.transition.assert_awaited_once()
    updates = repo.transition.call_args[0][0]
    cand = updates["embedding.candidate"]
    assert cand["name"] == "bge-m3-v2"
    assert cand["index_name"] == "chunks_v2"
    assert "promoted_at" in cand

    assert result["state"] == "CANDIDATE"


async def test_promote_rejected_when_state_not_idle() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    reg = _FakeRegistry(state="CANDIDATE", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with pytest.raises(IllegalEmbeddingTransition):
        await svc.promote(name="x", dim=512, api_url="u", model_arg="x")


async def test_promote_new_index_mapping_contains_embedding_with_correct_dim() -> None:
    """promote() creates the new index with a single 'embedding' field at the specified dim."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    es = AsyncMock()
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        AsyncMock(), es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")

    body = es.indices.create.call_args.kwargs["body"]
    emb = body["mappings"]["properties"]["embedding"]
    assert emb["type"] == "dense_vector"
    assert emb["dims"] == 768  # dim substituted from 1024 → 768


async def test_promote_passes_optimistic_lock_expect_candidate_null() -> None:
    """Promote must abort if another admin slipped a candidate in concurrently."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")

    expect = repo.transition.call_args.kwargs.get("expect") or repo.transition.call_args[1].get(
        "expect"
    )
    assert expect == {"embedding.candidate": None}


# ---------------------------------------------------------------------------
# cutover
# ---------------------------------------------------------------------------


async def test_cutover_passes_preflight_and_flips_read() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = _passing_preflight_es()
    cand = {**_bgem3v2_with_promoted_at(secs_ago=60), "index_name": "chunks_v2"}
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand)

    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.cutover()

    repo.transition.assert_awaited_once()
    assert repo.transition.call_args[0][0] == {"embedding.read": "candidate"}
    assert result["state"] == "CUTOVER"


async def test_cutover_blocked_by_hard_gate_failure() -> None:
    from ragent.services.embedding.lifecycle import (
        CutoverPreflightFailed,
        EmbeddingLifecycleService,
    )

    es = AsyncMock()
    # coverage too low: stable=100, candidate=50 → ratio 50% < 99%
    es.count.side_effect = [{"count": 100}, {"count": 50}]
    cand = {**_bgem3v2_with_promoted_at(secs_ago=60), "index_name": "chunks_v2"}
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand)

    svc = EmbeddingLifecycleService(
        AsyncMock(), es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with pytest.raises(CutoverPreflightFailed) as exc_info:
        await svc.cutover()
    assert exc_info.value.report["pass"] is False


async def test_cutover_warmup_gate_reads_promoted_at_from_registry_raw() -> None:
    """Regression: the warmup gate compares now-vs-promoted_at. If the
    service reads from the projected `candidate_dict` (which drops
    `promoted_at`), elapsed is always 0 and the gate is permanently
    broken. Pin that the path goes through `candidate_raw`."""
    from ragent.services.embedding.lifecycle import (
        CutoverPreflightFailed,
        EmbeddingLifecycleService,
    )

    # promoted_at is OLDER than 2 × cache_ttl → warmup gate must PASS;
    # then we'll force coverage to fail to verify the gate ran and
    # picked up the real timestamp (not utcnow).
    es = AsyncMock()
    es.count.side_effect = [{"count": 100}, {"count": 50}]  # 50% coverage → fail
    cand = {**_bgem3v2_with_promoted_at(secs_ago=600), "index_name": "chunks_v2"}
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand)
    svc = EmbeddingLifecycleService(
        AsyncMock(), es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with pytest.raises(CutoverPreflightFailed) as exc_info:
        await svc.cutover()
    gates = {g["name"]: g for g in exc_info.value.report["gates"]}
    # warmup must pass — proves promoted_at flowed in (else elapsed≈0 and gate fails)
    assert gates["dual_write_warmup"]["pass"] is True
    assert gates["dual_write_warmup"]["detail"]["elapsed_seconds"] > 500


async def test_cutover_rejected_when_state_not_candidate() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )
    with pytest.raises(IllegalEmbeddingTransition):
        await svc.cutover()


# ---------------------------------------------------------------------------
# rollback / commit / abort
# ---------------------------------------------------------------------------


async def test_rollback_from_cutover_returns_to_candidate() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    reg = _FakeRegistry(state="CUTOVER", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        repo, AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.rollback()

    assert repo.transition.call_args[0][0] == {"embedding.read": "stable"}
    assert result["state"] == "CANDIDATE"


async def test_commit_from_cutover_promotes_candidate_to_stable() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    cand_live = _bgem3v2_with_promoted_at()
    stable_live = _bgem3()

    async def _get(key):
        return {"embedding.stable": stable_live, "embedding.candidate": cand_live}.get(key)

    repo.get.side_effect = _get
    reg = _FakeRegistry(state="CUTOVER", candidate=cand_live)
    svc = EmbeddingLifecycleService(
        repo, AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.commit()

    updates = repo.transition.call_args[0][0]
    assert updates["embedding.stable"]["name"] == "bge-m3-v2"
    assert updates["embedding.candidate"] is None
    assert updates["embedding.read"] == "stable"
    retired = updates["embedding.retired"]
    assert len(retired) == 1
    assert retired[0]["name"] == "bge-m3"
    assert retired[0]["cleanup_done"] is False
    assert "retired_at" in retired[0]
    assert result["state"] == "IDLE"


async def test_abort_from_candidate_retires_candidate() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    cand_live = _bgem3v2_with_promoted_at()

    async def _get(key):
        return {"embedding.candidate": cand_live}.get(key)

    repo.get.side_effect = _get
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand_live)
    svc = EmbeddingLifecycleService(
        repo, AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.abort()

    updates = repo.transition.call_args[0][0]
    assert updates["embedding.candidate"] is None
    retired = updates["embedding.retired"]
    assert len(retired) == 1
    assert retired[0]["name"] == "bge-m3-v2"
    assert result["state"] == "IDLE"


async def test_abort_rejected_from_cutover() -> None:
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    reg = _FakeRegistry(state="CUTOVER", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )
    with pytest.raises(IllegalEmbeddingTransition):
        await svc.abort()


# ---------------------------------------------------------------------------
# Boundary logs (00_rule.md §Service Boundary Logs)
# ---------------------------------------------------------------------------


async def test_promote_emits_started_and_completed_logs() -> None:
    import structlog

    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    es = AsyncMock()
    es.indices.get_mapping.return_value = {"chunks_v1": {"mappings": {"properties": {}}}}
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        AsyncMock(), es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with structlog.testing.capture_logs() as logs:
        await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e", model_arg="bge-m3-v2")

    events = [e["event"] for e in logs]
    assert "embedding.lifecycle.promote.started" in events
    assert "embedding.lifecycle.promote.completed" in events


async def test_promote_failure_emits_failed_log() -> None:
    import structlog

    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    reg = _FakeRegistry(state="CANDIDATE", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with structlog.testing.capture_logs() as logs, pytest.raises(IllegalEmbeddingTransition):
        await svc.promote(name="x", dim=512, api_url="u", model_arg="x")

    events = [e["event"] for e in logs]
    assert "embedding.lifecycle.promote.failed" in events
    failed = next(e for e in logs if e["event"] == "embedding.lifecycle.promote.failed")
    assert failed["error_code"] == "IllegalEmbeddingTransition"


async def test_commit_emits_started_and_completed_logs() -> None:
    import structlog

    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    reg = _FakeRegistry(state="CUTOVER", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with structlog.testing.capture_logs() as logs:
        await svc.commit()

    events = [e["event"] for e in logs]
    assert "embedding.lifecycle.commit.started" in events
    assert "embedding.lifecycle.commit.completed" in events


# ---------------------------------------------------------------------------
# T-EM-R.3 — promote creates physical index; abort deletes it
# ---------------------------------------------------------------------------


async def test_next_index_name_increments_version() -> None:
    from ragent.services.embedding.lifecycle import _next_index_name

    assert _next_index_name("chunks_v1") == "chunks_v2"
    assert _next_index_name("chunks_v2") == "chunks_v3"
    assert _next_index_name("my_index_v10") == "my_index_v11"
    assert _next_index_name("chunks") == "chunks_v2"


async def test_promote_creates_new_physical_index_not_put_mapping() -> None:
    """T-EM-R.3 — promote must call indices.create for chunks_v2, not put_mapping."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")

    es.indices.create.assert_awaited_once()
    assert es.indices.create.call_args.kwargs["index"] == "chunks_v2"
    es.indices.put_mapping.assert_not_awaited()


async def test_promote_stores_index_name_in_candidate_payload() -> None:
    """T-EM-R.3 — candidate payload must carry index_name=chunks_v2."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")

    updates = repo.transition.call_args[0][0]
    assert updates["embedding.candidate"]["index_name"] == "chunks_v2"


async def test_abort_deletes_candidate_physical_index() -> None:
    """T-EM-R.3 — abort must DELETE the candidate index before retiring it in DB."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    cand = {**_bgem3v2_with_promoted_at(), "index_name": "chunks_v2"}
    repo = AsyncMock()
    es = AsyncMock()
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand)
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.abort()

    es.indices.delete.assert_awaited_once()
    assert es.indices.delete.call_args.kwargs["index"] == "chunks_v2"


async def test_promote_compensating_delete_on_transition_failure() -> None:
    """T-EM-R.3 — if repo.transition raises after index creation, the orphan index is deleted."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    repo.transition.side_effect = RuntimeError("optimistic lock conflict")
    es = AsyncMock()
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with pytest.raises(RuntimeError):
        await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")

    es.indices.create.assert_awaited_once()
    es.indices.delete.assert_awaited_once()
    assert es.indices.delete.call_args.kwargs["index"] == "chunks_v2"


# ---------------------------------------------------------------------------
# T-EM-R.4 — cutover / rollback perform ES alias swap
# ---------------------------------------------------------------------------


def _passing_preflight_es() -> AsyncMock:
    """ES mock pre-configured so the coverage + warmup preflight gates pass."""
    es = AsyncMock()
    es.count.side_effect = [{"count": 100}, {"count": 100}]
    return es


async def test_cutover_performs_alias_swap_stable_to_candidate() -> None:
    """T-EM-R.4 — cutover must atomically swap the read alias from stable to candidate index."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = _passing_preflight_es()
    cand = {**_bgem3v2_with_promoted_at(secs_ago=60), "index_name": "chunks_v2"}
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand)
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.cutover()

    es.indices.update_aliases.assert_awaited_once()
    body = es.indices.update_aliases.call_args.kwargs["body"]
    actions = body["actions"]
    assert {"remove": {"index": "chunks_v1", "alias": "chunks_v1_active"}} in actions
    assert {"add": {"index": "chunks_v2", "alias": "chunks_v1_active"}} in actions


async def test_rollback_performs_alias_swap_candidate_to_stable() -> None:
    """T-EM-R.4 — rollback must atomically swap the read alias back to stable index."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    cand = {**_bgem3v2_with_promoted_at(), "index_name": "chunks_v2"}
    reg = _FakeRegistry(state="CUTOVER", candidate=cand)
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.rollback()

    es.indices.update_aliases.assert_awaited_once()
    body = es.indices.update_aliases.call_args.kwargs["body"]
    actions = body["actions"]
    assert {"remove": {"index": "chunks_v2", "alias": "chunks_v1_active"}} in actions
    assert {"add": {"index": "chunks_v1", "alias": "chunks_v1_active"}} in actions


async def test_cutover_skips_alias_swap_for_legacy_candidate() -> None:
    """T-EM-R.4 — legacy candidates (no index_name) skip alias swap; force=True bypasses."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    cand = _bgem3v2_with_promoted_at(secs_ago=60)
    reg = _FakeRegistry(state="CANDIDATE", candidate=cand)
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.cutover(force=True)

    es.indices.update_aliases.assert_not_awaited()


async def test_rollback_skips_alias_swap_for_legacy_candidate() -> None:
    """T-EM-R.4 — legacy candidates (no index_name) skip the alias swap on rollback."""
    from ragent.services.embedding.lifecycle import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    cand = _bgem3v2_with_promoted_at()
    reg = _FakeRegistry(state="CUTOVER", candidate=cand)
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.rollback()

    es.indices.update_aliases.assert_not_awaited()
