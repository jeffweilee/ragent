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


# --- T-FB.9: feedback retriever 3-way graph dispatch (B50) ---


def _build_with_feedback(join_mode: str, feedback_retriever):
    from unittest.mock import MagicMock

    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

    from ragent.pipelines.chat import build_retrieval_pipeline

    return build_retrieval_pipeline(
        embedder=MagicMock(),
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode=join_mode,
        feedback_retriever=feedback_retriever,
        feedback_weight=0.5,
    )


def _make_feedback_retriever():
    from unittest.mock import MagicMock

    from ragent.pipelines.chat import _FeedbackMemoryRetriever

    return _FeedbackMemoryRetriever(es_client=MagicMock(), doc_repo=MagicMock())


def test_rrf_with_feedback_has_three_retrievers_and_weighted_joiner():
    pipeline = _build_with_feedback("rrf", _make_feedback_retriever())
    names = set(pipeline.graph.nodes)
    assert {"vector_retriever", "bm25_retriever", "feedback_retriever", "joiner"} <= names
    weights = pipeline.get_component("joiner").weights
    # Haystack normalises weights to sum=1; vector and BM25 share equal share,
    # feedback is half of either (preserving the [1.0, 1.0, 0.5] ratio).
    assert weights is not None and len(weights) == 3
    assert abs(weights[0] - weights[1]) < 1e-9
    assert abs(weights[2] / weights[0] - 0.5) < 1e-9


def test_rrf_with_feedback_connects_feedback_LAST_so_weights_map_correctly():
    """PR #80 review (gemini high): joiner inputs are positional, matched to
    `weights` by index. Feedback MUST be connected after vector + BM25 so the
    third weight slot (the discounted one) lands on the feedback retriever,
    not on BM25.

    Asserts the connection edges into `joiner.documents` are in the expected
    sender order: vector → bm25 → feedback.
    """
    pipeline = _build_with_feedback("rrf", _make_feedback_retriever())
    # nx MultiDiGraph: in-edges of `joiner` with the data they carry.
    in_edges = list(pipeline.graph.in_edges("joiner", data=True))
    senders_to_documents = [
        src
        for src, _, data in in_edges
        if data.get("to_socket") and data["to_socket"].name == "documents"
    ]
    assert senders_to_documents == ["vector_retriever", "bm25_retriever", "feedback_retriever"]


def test_rrf_without_feedback_has_no_third_retriever():
    pipeline = _build_with_feedback("rrf", None)
    names = set(pipeline.graph.nodes)
    assert "feedback_retriever" not in names
    assert pipeline.get_component("joiner").weights is None


def test_vector_only_mode_ignores_feedback_flag():
    pipeline = _build_with_feedback("vector_only", _make_feedback_retriever())
    names = set(pipeline.graph.nodes)
    assert "feedback_retriever" not in names
    assert "joiner" not in names


def test_bm25_only_mode_ignores_feedback_flag():
    pipeline = _build_with_feedback("bm25_only", _make_feedback_retriever())
    names = set(pipeline.graph.nodes)
    assert "feedback_retriever" not in names
    assert "joiner" not in names


def test_concatenate_mode_does_not_attach_feedback_input():
    """concatenate fuses but feedback retriever is RRF-specific (B50)."""
    pipeline = _build_with_feedback("concatenate", _make_feedback_retriever())
    names = set(pipeline.graph.nodes)
    assert "feedback_retriever" not in names
    assert pipeline.get_component("joiner").weights is None
