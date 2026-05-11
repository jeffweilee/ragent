"""T7.3 — Golden 50-Q top-3 ≥70% retrieval accuracy (C7).

The recall threshold is meaningful only when the embedder/rerank/LLM
endpoints point at the real third-party services. Default e2e runs use
the WireMock stack which returns deterministic-but-trivial vectors —
running the test against that stack would either always pass or always
fail in a way that does not reflect retrieval quality.

To opt in: set ``RAGENT_E2E_GOLDEN_SET=1`` (and supply real endpoint env
vars before pytest starts). Otherwise the test is xfail(run=False) so it
appears in the report as a tracked deferral, not a silent skip.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import API_URL

GOLDEN = Path(__file__).parent / "golden_set.jsonl"
CORPUS_DIR = Path(__file__).parent / "golden_corpus"
THRESHOLD = 0.70

_GOLDEN_ENABLED = os.getenv("RAGENT_E2E_GOLDEN_SET") == "1"

pytestmark = pytest.mark.docker


def _load_golden() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]


def test_golden_set_file_has_50_rows() -> None:
    rows = _load_golden()
    assert len(rows) == 50
    for row in rows:
        assert {"q", "expected_doc_id"}.issubset(row.keys())


def test_golden_corpus_covers_all_expected_doc_ids() -> None:
    """Catches drift between golden_set.jsonl and golden_corpus/*.md."""
    expected = {row["expected_doc_id"] for row in _load_golden()}
    on_disk = {p.stem for p in CORPUS_DIR.glob("*.md")}
    missing = expected - on_disk
    assert not missing, f"corpus missing markdown for: {sorted(missing)}"


@pytest.fixture(scope="session")
def golden_corpus_loaded(running_stack) -> Iterator[None]:
    """Ingest every doc in golden_corpus/ and wait until all are READY.

    Session-scoped so the corpus is loaded once for the whole golden-set
    test run. Skipped automatically when the golden flag is off (the
    pytest.mark.xfail above never executes the dependent test).
    """
    doc_ids: list[str] = []
    for md_path in sorted(CORPUS_DIR.glob("*.md")):
        doc_id = md_path.stem
        resp = httpx.post(
            f"{API_URL}/ingest/v1",
            headers={"X-User-Id": "alice"},
            json={
                "ingest_type": "inline",
                "source_id": doc_id,
                "source_app": "golden",
                "source_title": doc_id,
                "mime_type": "text/markdown",
                "content": md_path.read_text(),
            },
            timeout=10,
        )
        resp.raise_for_status()
        doc_ids.append(resp.json()["document_id"])

    deadline = time.monotonic() + 120
    pending = set(doc_ids)
    while pending and time.monotonic() < deadline:
        for doc_id in list(pending):
            status = (
                httpx.get(
                    f"{API_URL}/ingest/v1/{doc_id}",
                    headers={"X-User-Id": "alice"},
                    timeout=5,
                )
                .json()
                .get("status")
            )
            if status == "READY":
                pending.discard(doc_id)
            elif status == "FAILED":
                pytest.fail(f"corpus doc {doc_id} ingest FAILED")
        if pending:
            time.sleep(1)
    if pending:
        pytest.fail(f"corpus docs did not reach READY: {sorted(pending)}")
    yield


@pytest.mark.xfail(
    condition=not _GOLDEN_ENABLED,
    run=False,
    reason="T7.3 SLO is meaningful only against real embedder/rerank/LLM "
    "endpoints; default WireMock stack returns trivial vectors. Set "
    "RAGENT_E2E_GOLDEN_SET=1 (with real endpoint env vars) to enforce.",
)
def test_golden_set_top3_accuracy_at_least_70pct(golden_corpus_loaded) -> None:
    """Counts a hit when expected_doc_id appears in /chat response sources[0..2]."""
    rows = _load_golden()
    hits = 0
    for row in rows:
        resp = httpx.post(
            f"{API_URL}/chat/v1",
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
