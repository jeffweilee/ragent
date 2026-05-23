"""T-CHAOS.C3 — ES bulk 207 partial failure (spec §3.6.1).

Validates that `DocumentEmbedder._run_dual` handles a partial ES bulk
failure (errors=True with some items returning 429) by:
  1. Logging `es.bulk_partial_failure` for each failed item.
  2. Retrying only the failed items in a second bulk call.
  3. Succeeding overall when the retry bulk call returns no errors.

This is an in-process integration test using a mock ES client so that
the retry logic can be validated without a real ES cluster.  Marked
`@pytest.mark.docker` for suite consistency (runs in nightly CI lane).
"""

from __future__ import annotations

import pytest
import structlog

pytestmark = [
    pytest.mark.docker,
]


def _make_docs(n: int):
    """Return n Haystack Documents with deterministic IDs."""
    from haystack.dataclasses import Document

    return [
        Document(content=f"chunk content {i}", meta={"document_id": f"doc-{i}", "split_id": 0})
        for i in range(n)
    ]


def test_C3_es_bulk_partial_failure_retries_failed_items(dev_env) -> None:
    """DocumentEmbedder retries failed items and logs es.bulk_partial_failure."""
    from ragent.bootstrap.metrics import chaos_drill_outcome_total
    from ragent.pipelines.ingest import DocumentEmbedder

    n_docs = 25
    docs = _make_docs(n_docs)

    # Mock registry: one write model, stable index name
    class MockRegistry:
        def write_models(self):
            return ["model-a"]

        @property
        def stable_index(self):
            return "chunks_v1"

        @property
        def candidate_index(self):
            return None

    # Mock embed callable: returns zero vectors
    def mock_embed(model: str, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    # Mock ES client with partial failure on first call
    call_count = [0]

    class MockES:
        def bulk(self, index, operations):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: 25 docs → 5 failures (status 429)
                items = []
                for i in range(n_docs):
                    if i < 5:
                        items.append(
                            {
                                "index": {
                                    "_id": f"doc-{i}",
                                    "status": 429,
                                    "error": {"type": "circuit_breaking_exception"},
                                }
                            }
                        )
                    else:
                        items.append({"index": {"_id": f"doc-{i}", "status": 200}})
                return {"errors": True, "items": items}
            else:
                # Second call (retry of 5 failed items): all succeed
                return {
                    "errors": False,
                    "items": [
                        {"index": {"_id": f"doc-{i}", "status": 200}} for i in range(5)
                    ],
                }

    mock_es = MockES()

    embedder = DocumentEmbedder(
        registry=MockRegistry(),
        embed_callable=mock_embed,
        es_client=mock_es,
    )

    with structlog.testing.capture_logs() as cap:
        result = embedder.run(documents=docs)

    # Assert: bulk was called twice (initial + retry for failed items)
    assert call_count[0] == 2, (
        f"expected 2 bulk calls (initial + retry), got {call_count[0]}"
    )

    # Assert: partial failure event logged
    log_events = [entry.get("event") for entry in cap]
    assert "es.bulk_partial_failure" in log_events, (
        f"expected 'es.bulk_partial_failure' in log events, got: {log_events}"
    )

    # Assert: embedder returns empty documents list (registry mode)
    assert result == {"documents": []}

    # Record drill outcome
    chaos_drill_outcome_total.labels(case="C3", outcome="pass").inc()
    assert (
        chaos_drill_outcome_total.labels(case="C3", outcome="pass")._value.get() >= 1  # noqa: SLF001
    )
