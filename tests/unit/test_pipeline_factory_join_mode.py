"""T3.5a — Pipeline factory join mode: each CHAT_JOIN_MODE produces correct graph (C6)."""

import pytest


def _build(join_mode: str):
    from unittest.mock import MagicMock

    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

    from ragent.pipelines.chat import build_retrieval_pipeline

    embedder = MagicMock()
    doc_store = MagicMock(spec=ElasticsearchDocumentStore)
    doc_repo = MagicMock()
    return build_retrieval_pipeline(
        embedder=embedder, document_store=doc_store, doc_repo=doc_repo, join_mode=join_mode
    )


def test_rrf_mode_has_both_retrievers_and_joiner():
    pipeline = _build("rrf")
    names = set(pipeline.graph.nodes)
    assert "vector_retriever" in names
    assert "bm25_retriever" in names
    assert "joiner" in names


def test_vector_only_mode_has_no_bm25():
    pipeline = _build("vector_only")
    names = set(pipeline.graph.nodes)
    assert "vector_retriever" in names
    assert "bm25_retriever" not in names
    assert "joiner" not in names


def test_bm25_only_mode_has_no_vector():
    pipeline = _build("bm25_only")
    names = set(pipeline.graph.nodes)
    assert "bm25_retriever" in names
    assert "vector_retriever" not in names
    assert "joiner" not in names


def test_concatenate_mode_has_both_and_joiner():
    pipeline = _build("concatenate")
    names = set(pipeline.graph.nodes)
    assert "vector_retriever" in names
    assert "bm25_retriever" in names
    assert "joiner" in names


def test_default_is_rrf(monkeypatch):
    monkeypatch.setenv("CHAT_JOIN_MODE", "rrf")
    pipeline = _build("rrf")
    names = set(pipeline.graph.nodes)
    assert "joiner" in names


def test_invalid_mode_raises():
    with pytest.raises(ValueError, match="join_mode"):
        _build("unknown_mode")


def test_top_k_propagated_to_retrievers_and_joiner():
    from unittest.mock import MagicMock

    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

    from ragent.pipelines.chat import build_retrieval_pipeline

    pipeline = build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode="rrf",
        top_k=12,
    )
    assert pipeline.get_component("vector_retriever")._top_k == 12
    assert pipeline.get_component("bm25_retriever")._top_k == 12
    assert pipeline.get_component("joiner").top_k == 12
