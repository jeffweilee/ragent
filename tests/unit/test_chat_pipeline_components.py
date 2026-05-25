"""Unit tests for _Reranker and _LLMGenerator components (F1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from haystack.dataclasses import Document
from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

from ragent.pipelines.retrieve import _LLMGenerator, _Reranker, build_retrieval_pipeline


def test_reranker_reorders_documents_by_score() -> None:
    rerank_client = MagicMock()
    # bge-reranker returns highest first; reorder docs[1], docs[2], docs[0]
    rerank_client.rerank.return_value = [
        {"index": 1, "score": 0.9},
        {"index": 2, "score": 0.5},
        {"index": 0, "score": 0.1},
    ]
    docs = [
        Document(id="A", content="alpha"),
        Document(id="B", content="bravo"),
        Document(id="C", content="charlie"),
    ]
    out = _Reranker(rerank_client, top_k=3).run(query="q", documents=docs)["documents"]
    assert [d.id for d in out] == ["B", "C", "A"]


def test_reranker_top_k_caps_results() -> None:
    rerank_client = MagicMock()
    rerank_client.rerank.return_value = [
        {"index": 1, "score": 0.9},
        {"index": 0, "score": 0.5},
    ]
    docs = [Document(id="A"), Document(id="B"), Document(id="C")]
    out = _Reranker(rerank_client, top_k=2).run(query="q", documents=docs)["documents"]
    assert len(out) == 2
    assert [d.id for d in out] == ["B", "A"]


def test_reranker_empty_docs_short_circuits() -> None:
    rerank_client = MagicMock()
    out = _Reranker(rerank_client, top_k=5).run(query="q", documents=[])["documents"]
    assert out == []
    rerank_client.rerank.assert_not_called()


def test_llm_generator_returns_answer_and_passes_through_documents() -> None:
    llm_client = MagicMock()
    llm_client.chat.return_value = {
        "content": "the answer",
        "usage": {"promptTokens": 10, "completionTokens": 3, "totalTokens": 13},
    }
    docs = [Document(content="evidence")]
    result = _LLMGenerator(llm_client).run(
        messages=[{"role": "user", "content": "q"}], documents=docs, model="gpt-test"
    )
    assert result["answer"] == "the answer"
    assert result["documents"] == docs
    assert result["usage"]["totalTokens"] == 13


def test_build_retrieval_pipeline_with_rerank_inserts_reranker() -> None:
    rerank_client = MagicMock()
    pipeline = build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode="rrf",
        rerank_client=rerank_client,
    )
    assert "reranker" in pipeline.graph.nodes


def test_build_retrieval_pipeline_without_rerank_omits_reranker() -> None:
    pipeline = build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode="rrf",
    )
    assert "reranker" not in pipeline.graph.nodes


@pytest.mark.parametrize("mode", ["vector_only", "bm25_only"])
def test_reranker_works_in_single_retriever_modes(mode: str) -> None:
    rerank_client = MagicMock()
    pipeline = build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode=mode,
        rerank_client=rerank_client,
    )
    assert "reranker" in pipeline.graph.nodes


def test_reranker_logs_invalid_indices() -> None:
    """Out-of-bounds rerank indices are silently dropped — must surface as a warning."""
    import structlog.testing

    rerank_client = MagicMock()
    rerank_client.rerank.return_value = [
        {"index": 0, "score": 0.9},
        {"index": 5, "score": 0.5},  # out of bounds
        {"index": -1, "score": 0.4},  # invalid
        {"index": None, "score": 0.3},  # invalid
    ]
    docs = [Document(id="A"), Document(id="B")]
    with structlog.testing.capture_logs() as logs:
        out = _Reranker(rerank_client, top_k=4).run(query="q", documents=docs)["documents"]
    assert [d.id for d in out] == ["A"]
    assert any(e.get("event") == "rerank.invalid_indices" for e in logs)


def test_reranker_rejects_bool_index() -> None:
    """bool is a subclass of int; documents[True] would silently mean documents[1]."""
    rerank_client = MagicMock()
    rerank_client.rerank.return_value = [{"index": True, "score": 0.9}]
    docs = [Document(id="A"), Document(id="B")]
    out = _Reranker(rerank_client, top_k=1).run(query="q", documents=docs)["documents"]
    assert out == []


def test_reranker_run_accepts_runtime_top_k_overrides_construction_default() -> None:
    """T-APL.1 — per-request top_k must reach the rerank API call, not the build-time default.

    Without the runtime kwarg, run_retrieval's top_k is dropped on the floor at
    the reranker boundary: the rerank API is called with self._top_k (e.g. 20)
    even when the user asked for 2. Final response is still trimmed downstream,
    so the user-visible answer length is correct — but the rerank invocation
    pays for ranking ~10× more candidates than asked, and the rerank-ordered
    list reaching the RRF joiner has a different score distribution than it
    would under the requested top_k.
    """
    rerank_client = MagicMock()
    rerank_client.rerank.return_value = [
        {"index": 0, "score": 0.9},
        {"index": 1, "score": 0.5},
    ]
    docs = [Document(id="A"), Document(id="B"), Document(id="C")]
    out = _Reranker(rerank_client, top_k=20).run(query="q", documents=docs, top_k=2)["documents"]
    rerank_client.rerank.assert_called_once_with(query="q", texts=["", "", ""], top_k=2)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# P2.3 — fail-open behaviour when reranker raises UpstreamServiceError
# ---------------------------------------------------------------------------


def test_reranker_fail_open_returns_original_docs_on_service_error() -> None:
    """UpstreamServiceError → return original docs (capped to top_k), log warning."""
    import structlog.testing

    from ragent.errors.upstream import UpstreamServiceError

    rerank_client = MagicMock()
    rerank_client.rerank.side_effect = UpstreamServiceError("rerank 5xx", service="rerank")
    docs = [Document(id=str(i)) for i in range(5)]
    with structlog.testing.capture_logs() as logs:
        out = _Reranker(rerank_client, top_k=3).run(query="q", documents=docs)["documents"]
    assert [d.id for d in out] == ["0", "1", "2"]  # first top_k docs, original order
    assert any(e.get("event") == "rerank.degraded" for e in logs)


def test_reranker_fail_open_returns_original_docs_on_timeout() -> None:
    """UpstreamTimeoutError (subclass of UpstreamServiceError) → same fail-open path."""
    import structlog.testing

    from ragent.errors.upstream import UpstreamTimeoutError

    rerank_client = MagicMock()
    rerank_client.rerank.side_effect = UpstreamTimeoutError("rerank timeout", service="rerank")
    docs = [Document(id="X"), Document(id="Y")]
    with structlog.testing.capture_logs() as logs:
        out = _Reranker(rerank_client, top_k=5).run(query="q", documents=docs)["documents"]
    assert [d.id for d in out] == ["X", "Y"]  # all docs, top_k >= len(docs)
    assert any(e.get("event") == "rerank.degraded" for e in logs)


def test_reranker_fail_open_increments_degraded_metric_5xx() -> None:
    """UpstreamServiceError increments rerank_degraded_total{reason='5xx'}."""
    from prometheus_client import REGISTRY

    from ragent.errors.upstream import UpstreamServiceError

    before = REGISTRY.get_sample_value("rerank_degraded_total", {"reason": "5xx"}) or 0.0
    rerank_client = MagicMock()
    rerank_client.rerank.side_effect = UpstreamServiceError("err", service="rerank")
    _Reranker(rerank_client, top_k=1).run(query="q", documents=[Document(id="A")])
    after = REGISTRY.get_sample_value("rerank_degraded_total", {"reason": "5xx"}) or 0.0
    assert after == before + 1.0


def test_reranker_fail_open_increments_degraded_metric_timeout() -> None:
    """UpstreamTimeoutError increments rerank_degraded_total{reason='timeout'}."""
    from prometheus_client import REGISTRY

    from ragent.errors.upstream import UpstreamTimeoutError

    before = REGISTRY.get_sample_value("rerank_degraded_total", {"reason": "timeout"}) or 0.0
    rerank_client = MagicMock()
    rerank_client.rerank.side_effect = UpstreamTimeoutError("timeout", service="rerank")
    _Reranker(rerank_client, top_k=1).run(query="q", documents=[Document(id="B")])
    after = REGISTRY.get_sample_value("rerank_degraded_total", {"reason": "timeout"}) or 0.0
    assert after == before + 1.0


def test_reranker_reraises_on_4xx_reranker_error() -> None:
    """4xx HTTP errors (auth/config failures) must propagate — not silently fail-open.

    A reranker 401/403/422 means the operator has misconfigured credentials or the
    request contract is broken; treating it as fail-open would mask the defect.
    Only 5xx / timeout warrant RRF fallback (spec §3.6.1 C4).
    """
    from ragent.errors.upstream import UpstreamServiceError

    rerank_client = MagicMock()
    # Build a realistic UpstreamServiceError wrapping an httpx 401 response.
    mock_request = MagicMock(spec=httpx.Request)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 401
    http_exc = httpx.HTTPStatusError("401 Unauthorized", request=mock_request, response=mock_response)
    upstream_exc = UpstreamServiceError("rerank auth failure", service="rerank")
    upstream_exc.__cause__ = http_exc
    rerank_client.rerank.side_effect = upstream_exc

    docs = [Document(id="A")]
    with pytest.raises(UpstreamServiceError):
        _Reranker(rerank_client, top_k=1).run(query="q", documents=docs)
