"""Cutover preflight (T-EM.11, B50 §6).

Pure-ish function: takes the registry, ES client, index name, and the
candidate's `promoted_at` timestamp; returns a structured report. The
lifecycle service maps `pass=False` (with any hard gate failing) to a
409 problem-details response on the admin router.

Hard gates:
- state_is_candidate
- field_dim_matches
- candidate_coverage (≥ 99%, with empty-index escape)
- dual_write_warmup  (now - promoted_at ≥ 2 × cache_ttl)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ragent.utility.datetime import utcnow

_COVERAGE_THRESHOLD = 0.99


def _gate(name: str, level: str, passed: bool, **detail: Any) -> dict:
    return {"name": name, "level": level, "pass": passed, "detail": detail}


async def _gate_field_dim(
    es_client: Any, index_name: str, field: str | None, expected_dim: int | None
) -> dict:
    actual_dim: int | None = None
    if field is not None:
        mapping = await es_client.indices.get_mapping(index=index_name)
        try:
            actual_dim = mapping[index_name]["mappings"]["properties"][field]["dims"]
        except (KeyError, TypeError):
            actual_dim = None
    return _gate(
        "field_dim_matches",
        "hard",
        actual_dim is not None and actual_dim == expected_dim,
        expected_dim=expected_dim,
        actual_dim=actual_dim,
    )


async def _gate_coverage(es_client: Any, index_name: str, field: str | None) -> dict:
    if field is None:
        return _gate("candidate_coverage", "hard", False, detail="no_candidate")
    total = (await es_client.count(index=index_name))["count"]
    covered = (
        await es_client.count(index=index_name, body={"query": {"exists": {"field": field}}})
    )["count"]
    ratio = 1.0 if total == 0 else covered / total
    passed = total == 0 or ratio >= _COVERAGE_THRESHOLD
    return _gate(
        "candidate_coverage",
        "hard",
        passed,
        covered=covered,
        total=total,
        ratio=ratio,
        threshold=_COVERAGE_THRESHOLD,
    )


def _gate_warmup(promoted_at: datetime, cache_ttl_seconds: int) -> dict:
    required = 2 * cache_ttl_seconds
    elapsed = (utcnow() - promoted_at).total_seconds()
    return _gate(
        "dual_write_warmup",
        "hard",
        elapsed >= required,
        elapsed_seconds=elapsed,
        required_seconds=required,
    )


async def preflight(
    *,
    registry: Any,
    es_client: Any,
    index_name: str,
    promoted_at: datetime,
    cache_ttl_seconds: int,
) -> dict:
    state = registry.derived_state()
    state_ok = state == "CANDIDATE"
    gates: list[dict] = [_gate("state_is_candidate", "hard", state_ok, current_state=state)]
    if not state_ok:
        return {"pass": False, "gates": gates}

    candidate = registry.candidate_model()
    field = candidate.field if candidate else None
    expected_dim = candidate.dim if candidate else None

    gates.append(await _gate_field_dim(es_client, index_name, field, expected_dim))
    gates.append(await _gate_coverage(es_client, index_name, field))
    gates.append(_gate_warmup(promoted_at, cache_ttl_seconds))

    overall = all(g["pass"] for g in gates if g["level"] == "hard")
    return {"pass": overall, "gates": gates}
