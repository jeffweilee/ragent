"""T-EM.16 / T-EM-R.7 — _QueryEmbedder static embedding_field.

Contract:
- Registry mode: _QueryEmbedder always returns embedding_field="embedding"
  (static) regardless of which model read_model() returns. The alias routes
  kNN to the correct physical index transparently.
- The query is still embedded using registry.read_model() — the model choice
  determines the vector, not the field name.
- read_model() is called fresh per run() for live-cutover propagation.
- _FeedbackMemoryRetriever is constructed with the read alias (chunks_read_alias)
  so ES queries always target the live physical index.
"""

from unittest.mock import MagicMock

from ragent.clients.embedding_model_config import EmbeddingModelConfig


def _model(name: str, dim: int) -> EmbeddingModelConfig:
    return EmbeddingModelConfig(name=name, dim=dim, api_url="http://test", model_arg=name)


def _registry(read: EmbeddingModelConfig) -> MagicMock:
    reg = MagicMock()
    reg.read_model.return_value = read
    return reg


# ---------------------------------------------------------------------------
# Registry mode — static "embedding" field
# ---------------------------------------------------------------------------


def test_registry_mode_always_emits_static_embedding_field_idle() -> None:
    """T-EM-R.7 — IDLE: stable model used, but embedding_field is always 'embedding'."""
    from ragent.pipelines.chat import _QueryEmbedder

    stable = _model("bge-m3", 1024)

    qe = _QueryEmbedder(
        registry=_registry(stable),
        embed_callable=lambda m, texts: [[0.5] * m.dim],
    )
    result = qe.run(query="what is rag?")

    assert result["query"] == "what is rag?"
    assert result["query_embedding"] == [0.5] * 1024
    assert result["embedding_field"] == "embedding"


def test_registry_mode_always_emits_static_embedding_field_cutover() -> None:
    """T-EM-R.7 — CUTOVER: candidate model used, embedding_field still 'embedding'."""
    from ragent.pipelines.chat import _QueryEmbedder

    candidate = _model("bge-m3-v2", 768)

    qe = _QueryEmbedder(
        registry=_registry(candidate),
        embed_callable=lambda m, texts: [[1.0] * m.dim],
    )
    result = qe.run(query="hello")

    assert result["query_embedding"] == [1.0] * 768
    assert result["embedding_field"] == "embedding"


def test_registry_mode_embeds_with_read_model() -> None:
    """The model used to embed the query comes from registry.read_model()."""
    from ragent.pipelines.chat import _QueryEmbedder

    stable = _model("bge-m3", 1024)
    calls: list[tuple[str, list[str]]] = []

    def embed_callable(model, texts):
        calls.append((model.name, list(texts)))
        return [[0.5] * model.dim]

    qe = _QueryEmbedder(registry=_registry(stable), embed_callable=embed_callable)
    qe.run(query="what is rag?")

    assert calls == [("bge-m3", ["what is rag?"])]


def test_registry_mode_refreshes_read_model_per_call() -> None:
    """read_model() is called fresh per run() so a live cutover takes effect."""
    from ragent.pipelines.chat import _QueryEmbedder

    stable = _model("bge-m3", 1024)
    candidate = _model("bge-m3-v2", 768)
    reg = MagicMock()
    reg.read_model.side_effect = [stable, candidate]

    qe = _QueryEmbedder(
        registry=reg,
        embed_callable=lambda m, texts: [[1.0] * m.dim],
    )
    first = qe.run(query="q1")
    second = qe.run(query="q2")

    # Field always "embedding" regardless of which model was used.
    assert first["embedding_field"] == "embedding"
    assert second["embedding_field"] == "embedding"
    # But vectors have different lengths (from different models).
    assert len(first["query_embedding"]) == 1024
    assert len(second["query_embedding"]) == 768
    assert reg.read_model.call_count == 2


# ---------------------------------------------------------------------------
# Legacy mode (back-compat)
# ---------------------------------------------------------------------------


def test_legacy_mode_preserves_pre_em_signature() -> None:
    from ragent.pipelines.chat import _QueryEmbedder

    client = MagicMock()
    client.embed.return_value = [[0.25] * 1024]

    qe = _QueryEmbedder(client)
    result = qe.run(query="legacy")

    assert result["query"] == "legacy"
    assert result["query_embedding"] == [0.25] * 1024
    client.embed.assert_called_once_with(["legacy"], query=True)


def test_legacy_mode_does_not_emit_embedding_field() -> None:
    from ragent.pipelines.chat import _QueryEmbedder

    client = MagicMock()
    client.embed.return_value = [[0.0] * 1024]

    qe = _QueryEmbedder(client)
    result = qe.run(query="x")

    assert "embedding_field" not in result


# ---------------------------------------------------------------------------
# _DynamicFieldEmbeddingRetriever — still accepts field, now always "embedding"
# ---------------------------------------------------------------------------


def test_dynamic_retriever_uses_embedding_field_from_query_embedder() -> None:
    """T-EM-R.7 — downstream retriever receives 'embedding' from _QueryEmbedder."""
    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    store = MagicMock()
    store._search_documents.return_value = [MagicMock()]

    retriever = _DynamicFieldEmbeddingRetriever(document_store=store, top_k=10)
    retriever.run(query_embedding=[0.1] * 1024, embedding_field="embedding")

    body = store._search_documents.call_args.kwargs
    assert body["knn"]["field"] == "embedding"
    assert body["knn"]["query_vector"] == [0.1] * 1024
    assert body["knn"]["k"] == 10


def test_dynamic_retriever_defaults_field_to_embedding_when_omitted() -> None:
    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    store = MagicMock()
    store._search_documents.return_value = []

    retriever = _DynamicFieldEmbeddingRetriever(document_store=store, top_k=10)
    retriever.run(query_embedding=[0.0] * 4)

    body = store._search_documents.call_args.kwargs
    assert body["knn"]["field"] == "embedding"


def test_dynamic_retriever_passes_top_k_override_and_filters() -> None:
    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    store = MagicMock()
    store._search_documents.return_value = []

    retriever = _DynamicFieldEmbeddingRetriever(document_store=store, top_k=10)
    retriever.run(
        query_embedding=[0.1] * 4,
        embedding_field="embedding",
        top_k=25,
        filters={"field": "source_app", "operator": "==", "value": "confluence"},
    )

    body = store._search_documents.call_args.kwargs
    assert body["knn"]["k"] == 25
    assert body["knn"]["filter"] == {"bool": {"must": {"term": {"source_app": "confluence"}}}}


def test_dynamic_retriever_normalises_composite_filters() -> None:
    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    store = MagicMock()
    store._search_documents.return_value = []

    retriever = _DynamicFieldEmbeddingRetriever(document_store=store, top_k=10)
    retriever.run(
        query_embedding=[0.1] * 4,
        embedding_field="embedding",
        filters={
            "operator": "AND",
            "conditions": [
                {"field": "source_app", "operator": "==", "value": "confluence"},
                {"field": "source_meta", "operator": "==", "value": "space-A"},
            ],
        },
    )

    body = store._search_documents.call_args.kwargs
    assert body["knn"]["filter"] == {
        "bool": {
            "must": [
                {"term": {"source_app": "confluence"}},
                {"term": {"source_meta": "space-A"}},
            ]
        }
    }


def test_dynamic_retriever_rejects_empty_embedding() -> None:
    import pytest

    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    retriever = _DynamicFieldEmbeddingRetriever(document_store=MagicMock(), top_k=10)
    with pytest.raises(ValueError, match="non-empty"):
        retriever.run(query_embedding=[], embedding_field="embedding")


# ---------------------------------------------------------------------------
# _FeedbackMemoryRetriever — uses read alias, not physical index name
# ---------------------------------------------------------------------------


def test_feedback_retriever_queries_es_with_provided_chunks_index() -> None:
    """T-EM-R.7 — when constructed with a read alias, ES queries use that alias."""
    from unittest.mock import AsyncMock

    from ragent.pipelines.chat import _FeedbackMemoryRetriever

    es = MagicMock()
    doc_repo = AsyncMock()
    retriever = _FeedbackMemoryRetriever(
        es_client=es,
        doc_repo=doc_repo,
        chunks_index="chunks_v1_active",
        min_votes=1,
        half_life_days=14,
        request_timeout=10.0,
    )

    assert retriever._chunks_index == "chunks_v1_active"
