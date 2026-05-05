"""T7.3 — Golden 50-Q top-3 ≥70% retrieval accuracy (C7)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import API_URL

pytestmark = pytest.mark.docker

GOLDEN = Path(__file__).parent / "golden_set.jsonl"
THRESHOLD = 0.70


def _load_golden() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]


def test_golden_set_file_has_50_rows() -> None:
    rows = _load_golden()
    assert len(rows) == 50
    for row in rows:
        assert {"q", "expected_doc_id"}.issubset(row.keys())


def test_golden_set_top3_accuracy_at_least_70pct() -> None:
    """Top-3 retrieval recall against the launched API process from T7.2.
    Counts a hit when expected_doc_id appears in /chat response sources[0..2]."""
    rows = _load_golden()
    hits = 0
    for row in rows:
        resp = httpx.post(
            f"{API_URL}/chat",
            headers={"X-User-Id": "alice"},
            json={"query": row["q"], "top_k": 3},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        sources = [s.get("document_id") for s in body.get("sources", [])][:3]
        if row["expected_doc_id"] in sources:
            hits += 1
    accuracy = hits / len(rows)
    assert accuracy >= THRESHOLD, f"top-3 accuracy {accuracy:.2%} < {THRESHOLD:.0%}"
