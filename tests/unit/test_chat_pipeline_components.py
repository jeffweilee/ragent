"""Unit tests for _Reranker and _LLMGenerator components (F1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from haystack.dataclasses import Document
from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

from ragent.pipelines.chat import _LLMGenerator, _Reranker, build_retrieval_pipeline


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
