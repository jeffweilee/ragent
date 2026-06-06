"""T-EM.10 / T-EM-R.5 — Cutover preflight (B50 §6).

Hard gates (cutover refuses if any fails):
- state_is_candidate     — current state must be CANDIDATE
- candidate_coverage     — count(candidate_index) / count(stable_index) ≥ 0.99
- dual_write_warmup      — (now - candidate.promoted_at) ≥ 2 × cache_ttl_seconds

`preflight(...)` returns a structured report (list of gate results) AND a
`pass` boolean. The lifecycle service maps pass=False to 409 Conflict.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_registry(
    state="CANDIDATE",
    candidate_index="chunks_v2",
    stable_index="chunks_v1",
):
    reg = MagicMock()
    reg.derived_state.return_value = state
    reg.candidate_index = candidate_index
    reg.stable_index = stable_index
    return reg


def _passing_es(stable_count=100_000, candidate_count=100_000) -> AsyncMock:
    es = AsyncMock()
    es.count.side_effect = [{"count": stable_count}, {"count": candidate_count}]
    return es


# ---------------------------------------------------------------------------
# state_is_candidate
# ---------------------------------------------------------------------------


async def test_preflight_passes_when_state_is_candidate_and_all_gates_ok() -> None:
    from ragent.services.embedding.preflight import preflight

    report = await preflight(
        registry=_make_registry(),
        es_client=_passing_es(),
        promoted_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    assert report["pass"] is True
    assert all(g["pass"] for g in report["gates"] if g["level"] == "hard")


async def test_preflight_fails_when_state_is_not_candidate() -> None:
    from ragent.services.embedding.preflight import preflight

    reg = _make_registry(state="IDLE")
    report = await preflight(
        registry=reg,
        es_client=AsyncMock(),
        promoted_at=datetime.now(timezone.utc),
        cache_ttl_seconds=10,
    )

    assert report["pass"] is False
    state_gate = next(g for g in report["gates"] if g["name"] == "state_is_candidate")
    assert state_gate["pass"] is False
    assert state_gate["level"] == "hard"


# ---------------------------------------------------------------------------
# candidate_coverage (T-EM-R.5 — index doc-count comparison)
# ---------------------------------------------------------------------------


async def test_preflight_coverage_counts_candidate_index_not_field_exists() -> None:
    """T-EM-R.5 — coverage gate calls count(candidate_index), never a field-exists query."""
    from ragent.services.embedding.preflight import preflight

    reg = _make_registry()
    es = AsyncMock()
    es.count.side_effect = [
        {"count": 100},  # stable_index total
        {"count": 100},  # candidate_index total
    ]

    await preflight(
        registry=reg,
        es_client=es,
        promoted_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    calls = es.count.call_args_list
    assert any(c.kwargs.get("index") == "chunks_v2" for c in calls), (
        "coverage gate must query candidate_index"
    )
    assert all("body" not in c.kwargs for c in calls), (
        "coverage gate must not use field-exists filter"
    )


async def test_preflight_fails_when_candidate_coverage_below_threshold() -> None:
    from ragent.services.embedding.preflight import preflight

    es = AsyncMock()
    es.count.side_effect = [
        {"count": 100_000},  # stable_index total
        {"count": 90_000},  # candidate_index (90% — below 99% threshold)
    ]

    report = await preflight(
        registry=_make_registry(),
        es_client=es,
        promoted_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    cov_gate = next(g for g in report["gates"] if g["name"] == "candidate_coverage")
    assert cov_gate["pass"] is False
    assert cov_gate["detail"]["ratio"] == pytest.approx(0.9)
    assert report["pass"] is False


async def test_preflight_coverage_treats_empty_stable_index_as_pass() -> None:
    """Empty stable index → ratio undefined → treat as pass (initial promote, no docs yet)."""
    from ragent.services.embedding.preflight import preflight

    es = AsyncMock()
    es.count.side_effect = [{"count": 0}, {"count": 0}]

    report = await preflight(
        registry=_make_registry(),
        es_client=es,
        promoted_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    cov_gate = next(g for g in report["gates"] if g["name"] == "candidate_coverage")
    assert cov_gate["pass"] is True


async def test_preflight_coverage_fails_when_candidate_index_is_none() -> None:
    """T-EM-R.5 — coverage gate fails when candidate_index is None (legacy candidate)."""
    from ragent.services.embedding.preflight import preflight

    reg = _make_registry(candidate_index=None)
    es = AsyncMock()

    report = await preflight(
        registry=reg,
        es_client=es,
        promoted_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    cov_gate = next(g for g in report["gates"] if g["name"] == "candidate_coverage")
    assert cov_gate["pass"] is False
    assert report["pass"] is False


# ---------------------------------------------------------------------------
# dual_write_warmup
# ---------------------------------------------------------------------------


async def test_preflight_fails_when_dual_write_warmup_too_short() -> None:
    from ragent.services.embedding.preflight import preflight

    report = await preflight(
        registry=_make_registry(),
        es_client=_passing_es(),
        promoted_at=datetime.now(timezone.utc) - timedelta(seconds=5),  # < 2 × ttl(10) = 20
        cache_ttl_seconds=10,
    )

    warm_gate = next(g for g in report["gates"] if g["name"] == "dual_write_warmup")
    assert warm_gate["pass"] is False
    assert report["pass"] is False
