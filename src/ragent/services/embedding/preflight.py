"""Cutover preflight (T-EM.11, B50 §6).

Pure-ish function: takes the registry, ES client, and the candidate's
`promoted_at` timestamp; returns a structured report. The
lifecycle service maps `pass=False` (with any hard gate failing) to a
409 problem-details response on the admin router.

Hard gates:
- state_is_candidate
- candidate_coverage (≥ 99%, with empty-stable-index escape)
- dual_write_warmup  (now - promoted_at ≥ 2 × cache_ttl)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ragent.utility.datetime import utcnow

_COVERAGE_THRESHOLD = 0.99


def _gate(name: str, level: str, passed: bool, **detail: Any) -> dict:
    return {"name": name, "level": level, "pass": passed, "detail": detail}


async def _gate_coverage(es_client: Any, stable_index: str, candidate_index: str | None) -> dict:
    if candidate_index is None:
        return _gate("candidate_coverage", "hard", False, detail="no_candidate_index")
    total = (await es_client.count(index=stable_index))["count"]
    covered = (await es_client.count(index=candidate_index))["count"]
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
    promoted_at: datetime,
    cache_ttl_seconds: int,
) -> dict:
    state = registry.derived_state()
    state_ok = state == "CANDIDATE"
    gates: list[dict] = [_gate("state_is_candidate", "hard", state_ok, current_state=state)]
    if not state_ok:
        return {"pass": False, "gates": gates}

    gates.append(await _gate_coverage(es_client, registry.stable_index, registry.candidate_index))
    gates.append(_gate_warmup(promoted_at, cache_ttl_seconds))

    overall = all(g["pass"] for g in gates if g["level"] == "hard")
    return {"pass": overall, "gates": gates}
