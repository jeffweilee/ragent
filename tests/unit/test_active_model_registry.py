"""T-EM.8 — ActiveModelRegistry (B50).

App-side cache of the four `embedding.*` settings rows. Polled from
`SystemSettingsRepository` on a TTL (default 10s) so admin lifecycle moves
take effect without an App restart.

Contract:
- `read_model()` → the single `EmbeddingModelConfig` queries should embed with
  (selected by `embedding.read = "stable" | "candidate"`).
- `write_models()` → `[stable]` in IDLE state; `[stable, candidate]` whenever
  candidate is non-null (dual-write window). Used by ingest pipeline.
- `derived_state()` → `"IDLE" | "CANDIDATE" | "CUTOVER"`.
- `refresh()` re-fetches all four settings rows.
- On refresh failure, last good cache is retained (stale-on-failure).
"""

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


def _bgem3v2() -> dict:
    return {
        "name": "bge-m3-v2",
        "dim": 768,
        "api_url": "http://e2",
        "model_arg": "bge-m3-v2",
        "field": "embedding_bgem3v2_768",
    }


def _mock_repo(stable=None, candidate=None, read="stable", retired=None):
    repo = AsyncMock()

    async def _get_many(keys):
        all_values = {
            "embedding.stable": stable if stable is not None else _bgem3(),
            "embedding.candidate": candidate,
            "embedding.read": read,
            "embedding.retired": retired if retired is not None else [],
        }
        return {k: all_values[k] for k in keys if k in all_values}

    repo.get_many.side_effect = _get_many
    return repo


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


async def test_state_is_idle_when_no_candidate() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(), ttl_seconds=999)
    await reg.refresh()
    assert reg.derived_state() == "IDLE"


async def test_state_is_candidate_when_candidate_set_and_read_stable() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(candidate=_bgem3v2(), read="stable"), ttl_seconds=999)
    await reg.refresh()
    assert reg.derived_state() == "CANDIDATE"


async def test_state_is_cutover_when_read_is_candidate() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(candidate=_bgem3v2(), read="candidate"), ttl_seconds=999)
    await reg.refresh()
    assert reg.derived_state() == "CUTOVER"


# ---------------------------------------------------------------------------
# read_model / write_models
# ---------------------------------------------------------------------------


async def test_read_model_returns_stable_when_read_is_stable() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(candidate=_bgem3v2(), read="stable"), ttl_seconds=999)
    await reg.refresh()
    m = reg.read_model()
    assert m.name == "bge-m3"
    assert m.dim == 1024
    assert m.field == "embedding_bgem3_1024"


async def test_read_model_returns_candidate_when_read_is_candidate() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(candidate=_bgem3v2(), read="candidate"), ttl_seconds=999)
    await reg.refresh()
    m = reg.read_model()
    assert m.name == "bge-m3-v2"
    assert m.dim == 768


async def test_write_models_includes_only_stable_in_idle() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(), ttl_seconds=999)
    await reg.refresh()
    write = reg.write_models()
    assert len(write) == 1
    assert write[0].name == "bge-m3"


async def test_write_models_includes_both_in_candidate_or_cutover() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(candidate=_bgem3v2(), read="stable"), ttl_seconds=999)
    await reg.refresh()
    write = reg.write_models()
    assert len(write) == 2
    assert {m.name for m in write} == {"bge-m3", "bge-m3-v2"}


# ---------------------------------------------------------------------------
# Cache TTL / refresh failure
# ---------------------------------------------------------------------------


async def test_refresh_failure_retains_last_good_cache() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    repo = _mock_repo()
    reg = ActiveModelRegistry(repo, ttl_seconds=999)
    await reg.refresh()

    repo.get_many.side_effect = RuntimeError("DB blip")
    # Refresh should not raise; cache stays warm.
    await reg.refresh()

    assert reg.read_model().name == "bge-m3"


async def test_refresh_failure_emits_stale_warning() -> None:
    import structlog

    from ragent.services.active_model_registry import ActiveModelRegistry

    repo = _mock_repo()
    reg = ActiveModelRegistry(repo, ttl_seconds=999)
    await reg.refresh()

    repo.get_many.side_effect = RuntimeError("DB blip")
    with structlog.testing.capture_logs() as logs:
        await reg.refresh()

    events = [e["event"] for e in logs]
    assert "embedding.cache.stale" in events


async def test_read_before_refresh_raises() -> None:
    from ragent.services.active_model_registry import (
        ActiveModelRegistry,
        ActiveModelRegistryNotReady,
    )

    reg = ActiveModelRegistry(_mock_repo(), ttl_seconds=999)
    with pytest.raises(ActiveModelRegistryNotReady):
        reg.read_model()


# ---------------------------------------------------------------------------
# Snapshot / promoted_at
# ---------------------------------------------------------------------------


async def test_snapshot_carries_state_and_models() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistry

    reg = ActiveModelRegistry(_mock_repo(candidate=_bgem3v2(), read="candidate"), ttl_seconds=999)
    await reg.refresh()
    snap = reg.snapshot()
    assert snap["state"] == "CUTOVER"
    assert snap["stable"]["name"] == "bge-m3"
    assert snap["candidate"]["name"] == "bge-m3-v2"
    assert snap["read"] == "candidate"
    assert snap["retired"] == []
