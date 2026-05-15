"""T-EM.10 — Cutover preflight (B50 §6).

Hard gates (cutover refuses if any fails):
- state_is_candidate     — current state must be CANDIDATE
- field_dim_matches      — ES mapping.dims for candidate.field == candidate.dim
- candidate_coverage     — chunks with candidate.field set / total chunks ≥ 0.99
- dual_write_warmup      — (now - candidate.promoted_at) ≥ 2 × cache_ttl_seconds

Soft gates (warn; bypassable via `force=True`):
- candidate_embed_health — Prometheus query placeholder, returns provided value
- recent_benchmark_passed — settings row indicates a recent benchmark pass

`preflight(...)` returns a structured report (list of gate results) AND a
`pass` boolean. The lifecycle service caller decides whether to allow the
cutover or 409 based on the report.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


def _bgem3v2_field_mapping(dim=768):
    return {
        "chunks_v1": {
            "mappings": {
                "properties": {"embedding_bgem3v2_768": {"type": "dense_vector", "dims": dim}}
            }
        }
    }


def _make_registry(state="CANDIDATE", candidate_field="embedding_bgem3v2_768", candidate_dim=768):
    reg = MagicMock()
    reg.derived_state.return_value = state
    cand = MagicMock()
    cand.field = candidate_field
    cand.dim = candidate_dim
    reg.candidate_model.return_value = cand
    return reg


# ---------------------------------------------------------------------------
# state_is_candidate
# ---------------------------------------------------------------------------


async def test_preflight_passes_when_state_is_candidate_and_all_gates_ok() -> None:
    from ragent.services.cutover_preflight import preflight

    reg = _make_registry()
    es = AsyncMock()
    es.indices.get_mapping.return_value = _bgem3v2_field_mapping()
    es.count.side_effect = [
        {"count": 100_000},  # total
        {"count": 100_000},  # covered
    ]

    report = await preflight(
        registry=reg,
        es_client=es,
        index_name="chunks_v1",
        promoted_at=datetime.now(UTC) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    assert report["pass"] is True
    assert all(g["pass"] for g in report["gates"] if g["level"] == "hard")


async def test_preflight_fails_when_state_is_not_candidate() -> None:
    from ragent.services.cutover_preflight import preflight

    reg = _make_registry(state="IDLE")
    report = await preflight(
        registry=reg,
        es_client=AsyncMock(),
        index_name="chunks_v1",
        promoted_at=datetime.now(UTC),
        cache_ttl_seconds=10,
    )

    assert report["pass"] is False
    state_gate = next(g for g in report["gates"] if g["name"] == "state_is_candidate")
    assert state_gate["pass"] is False
    assert state_gate["level"] == "hard"


# ---------------------------------------------------------------------------
# field_dim_matches
# ---------------------------------------------------------------------------


async def test_preflight_fails_when_field_dim_mismatch() -> None:
    from ragent.services.cutover_preflight import preflight

    reg = _make_registry()  # candidate.dim = 768
    es = AsyncMock()
    es.indices.get_mapping.return_value = _bgem3v2_field_mapping(dim=1024)  # mismatch!
    es.count.side_effect = [{"count": 100}, {"count": 100}]

    report = await preflight(
        registry=reg,
        es_client=es,
        index_name="chunks_v1",
        promoted_at=datetime.now(UTC) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    dim_gate = next(g for g in report["gates"] if g["name"] == "field_dim_matches")
    assert dim_gate["pass"] is False
    assert report["pass"] is False


# ---------------------------------------------------------------------------
# candidate_coverage
# ---------------------------------------------------------------------------


async def test_preflight_fails_when_candidate_coverage_below_threshold() -> None:
    from ragent.services.cutover_preflight import preflight

    reg = _make_registry()
    es = AsyncMock()
    es.indices.get_mapping.return_value = _bgem3v2_field_mapping()
    es.count.side_effect = [
        {"count": 100_000},  # total
        {"count": 90_000},  # covered (90% — below 99% threshold)
    ]

    report = await preflight(
        registry=reg,
        es_client=es,
        index_name="chunks_v1",
        promoted_at=datetime.now(UTC) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    cov_gate = next(g for g in report["gates"] if g["name"] == "candidate_coverage")
    assert cov_gate["pass"] is False
    assert cov_gate["detail"]["ratio"] == pytest.approx(0.9)
    assert report["pass"] is False


async def test_preflight_coverage_treats_empty_index_as_pass() -> None:
    """No chunks yet → ratio undefined → treat as pass (operator is doing initial promote)."""
    from ragent.services.cutover_preflight import preflight

    reg = _make_registry()
    es = AsyncMock()
    es.indices.get_mapping.return_value = _bgem3v2_field_mapping()
    es.count.side_effect = [{"count": 0}, {"count": 0}]

    report = await preflight(
        registry=reg,
        es_client=es,
        index_name="chunks_v1",
        promoted_at=datetime.now(UTC) - timedelta(seconds=60),
        cache_ttl_seconds=10,
    )

    cov_gate = next(g for g in report["gates"] if g["name"] == "candidate_coverage")
    assert cov_gate["pass"] is True


# ---------------------------------------------------------------------------
# dual_write_warmup
# ---------------------------------------------------------------------------


async def test_preflight_fails_when_dual_write_warmup_too_short() -> None:
    from ragent.services.cutover_preflight import preflight

    reg = _make_registry()
    es = AsyncMock()
    es.indices.get_mapping.return_value = _bgem3v2_field_mapping()
    es.count.side_effect = [{"count": 100}, {"count": 100}]

    report = await preflight(
        registry=reg,
        es_client=es,
        index_name="chunks_v1",
        promoted_at=datetime.now(UTC) - timedelta(seconds=5),  # < 2 × ttl(10) = 20
        cache_ttl_seconds=10,
    )

    warm_gate = next(g for g in report["gates"] if g["name"] == "dual_write_warmup")
    assert warm_gate["pass"] is False
    assert report["pass"] is False
