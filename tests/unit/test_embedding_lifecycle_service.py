"""T-EM.11 — EmbeddingLifecycleService orchestrates the five admin actions.

Each public method:
1. Reads current state via the registry (caller is responsible for `refresh()`
   having succeeded).
2. Asserts the state-machine transition; raises `IllegalEmbeddingTransition`
   on rejection (mapped to 409 in router).
3. Performs side effects: ES mapping PUT (promote only), settings transition.
4. Returns a snapshot dict for the router to echo.
"""

from datetime import UTC, datetime, timedelta
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
        "promoted_at": (datetime.now(UTC) - timedelta(seconds=secs_ago)).isoformat(),
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

    def candidate_model(self):
        if self._candidate_dict is None:
            return None
        from ragent.clients.embedding_model_config import EmbeddingModelConfig

        keys = ("name", "dim", "api_url", "model_arg")
        return EmbeddingModelConfig(**{k: self._candidate_dict[k] for k in keys})

    def stable_model(self):
        if self._stable_dict is None:
            return None
        from ragent.clients.embedding_model_config import EmbeddingModelConfig

        keys = ("name", "dim", "api_url", "model_arg")
        return EmbeddingModelConfig(**{k: self._stable_dict[k] for k in keys})


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


async def test_promote_from_idle_writes_candidate_and_puts_mapping() -> None:
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    es.indices.get_mapping.return_value = {"chunks_v1": {"mappings": {"properties": {}}}}
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.promote(
        name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2"
    )

    es.indices.put_mapping.assert_awaited_once()
    put_kwargs = es.indices.put_mapping.call_args.kwargs
    assert put_kwargs["index"] == "chunks_v1"
    props = put_kwargs["body"]["properties"]
    assert "embedding_bgem3v2_768" in props
    assert props["embedding_bgem3v2_768"]["dims"] == 768

    repo.transition.assert_awaited_once()
    updates = repo.transition.call_args[0][0]
    cand = updates["embedding.candidate"]
    assert cand["name"] == "bge-m3-v2"
    assert cand["field"] == "embedding_bgem3v2_768"
    assert "promoted_at" in cand

    assert result["state"] == "CANDIDATE"


async def test_promote_rejected_when_state_not_idle() -> None:
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    reg = _FakeRegistry(state="CANDIDATE", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with pytest.raises(IllegalEmbeddingTransition):
        await svc.promote(name="x", dim=512, api_url="u", model_arg="x")


async def test_promote_rejects_field_collision_when_dim_differs() -> None:
    from ragent.services.embedding_lifecycle_service import (
        EmbeddingFieldCollision,
        EmbeddingLifecycleService,
    )

    es = AsyncMock()
    # Field already present with a DIFFERENT dim — real collision.
    es.indices.get_mapping.return_value = {
        "chunks_v1": {
            "mappings": {
                "properties": {"embedding_bgem3v2_768": {"type": "dense_vector", "dims": 1024}}
            }
        }
    }
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        AsyncMock(), es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with pytest.raises(EmbeddingFieldCollision):
        await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")


async def test_promote_idempotent_when_field_exists_with_same_dim() -> None:
    """Retry-safe path: if a previous attempt PUT the mapping but failed at the
    settings transition, the operator can re-issue promote and we accept the
    pre-existing field as ours (same dim → idempotent)."""
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    es.indices.get_mapping.return_value = {
        "chunks_v1": {
            "mappings": {
                "properties": {"embedding_bgem3v2_768": {"type": "dense_vector", "dims": 768}}
            }
        }
    }
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.promote(
        name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2"
    )

    assert result["state"] == "CANDIDATE"
    repo.transition.assert_awaited_once()


async def test_promote_put_mapping_uses_flat_index_options() -> None:
    """B26 — Phase-1 mapping uses index_options.type=flat. Match it."""
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    es = AsyncMock()
    es.indices.get_mapping.return_value = {"chunks_v1": {"mappings": {"properties": {}}}}
    reg = _FakeRegistry(state="IDLE")
    svc = EmbeddingLifecycleService(
        AsyncMock(), es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    await svc.promote(name="bge-m3-v2", dim=768, api_url="http://e2", model_arg="bge-m3-v2")

    body = es.indices.put_mapping.call_args.kwargs["body"]
    field_def = body["properties"]["embedding_bgem3v2_768"]
    assert field_def["index_options"] == {"type": "flat"}


async def test_promote_passes_optimistic_lock_expect_candidate_null() -> None:
    """Promote must abort if another admin slipped a candidate in concurrently."""
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    es.indices.get_mapping.return_value = {"chunks_v1": {"mappings": {"properties": {}}}}
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
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    repo = AsyncMock()
    es = AsyncMock()
    es.indices.get_mapping.return_value = {
        "chunks_v1": {
            "mappings": {
                "properties": {"embedding_bgem3v2_768": {"type": "dense_vector", "dims": 768}}
            }
        }
    }
    es.count.side_effect = [{"count": 100}, {"count": 100}]
    reg = _FakeRegistry(state="CANDIDATE", candidate=_bgem3v2_with_promoted_at(secs_ago=60))

    svc = EmbeddingLifecycleService(
        repo, es, index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.cutover()

    repo.transition.assert_awaited_once()
    assert repo.transition.call_args[0][0] == {"embedding.read": "candidate"}
    assert result["state"] == "CUTOVER"


async def test_cutover_blocked_by_hard_gate_failure() -> None:
    from ragent.services.embedding_lifecycle_service import (
        CutoverPreflightFailed,
        EmbeddingLifecycleService,
    )

    es = AsyncMock()
    es.indices.get_mapping.return_value = {
        "chunks_v1": {
            "mappings": {
                "properties": {"embedding_bgem3v2_768": {"type": "dense_vector", "dims": 768}}
            }
        }
    }
    # coverage too low.
    es.count.side_effect = [{"count": 100}, {"count": 50}]
    reg = _FakeRegistry(state="CANDIDATE", candidate=_bgem3v2_with_promoted_at(secs_ago=60))

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
    from ragent.services.embedding_lifecycle_service import (
        CutoverPreflightFailed,
        EmbeddingLifecycleService,
    )

    # promoted_at is OLDER than 2 × cache_ttl → warmup gate must PASS;
    # then we'll force coverage to fail to verify the gate ran and
    # picked up the real timestamp (not utcnow).
    es = AsyncMock()
    es.indices.get_mapping.return_value = {
        "chunks_v1": {
            "mappings": {
                "properties": {"embedding_bgem3v2_768": {"type": "dense_vector", "dims": 768}}
            }
        }
    }
    es.count.side_effect = [{"count": 100}, {"count": 50}]  # 50% coverage → fail
    reg = _FakeRegistry(state="CANDIDATE", candidate=_bgem3v2_with_promoted_at(secs_ago=600))
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
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService
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
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    repo = AsyncMock()
    reg = _FakeRegistry(state="CUTOVER", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        repo, AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    result = await svc.rollback()

    assert repo.transition.call_args[0][0] == {"embedding.read": "stable"}
    assert result["state"] == "CANDIDATE"


async def test_commit_from_cutover_promotes_candidate_to_stable() -> None:
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

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
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

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
    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService
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

    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

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

    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService
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

    from ragent.services.embedding_lifecycle_service import EmbeddingLifecycleService

    reg = _FakeRegistry(state="CUTOVER", candidate=_bgem3v2_with_promoted_at())
    svc = EmbeddingLifecycleService(
        AsyncMock(), AsyncMock(), index_name="chunks_v1", registry=reg, cache_ttl_seconds=10
    )

    with structlog.testing.capture_logs() as logs:
        await svc.commit()

    events = [e["event"] for e in logs]
    assert "embedding.lifecycle.commit.started" in events
    assert "embedding.lifecycle.commit.completed" in events
