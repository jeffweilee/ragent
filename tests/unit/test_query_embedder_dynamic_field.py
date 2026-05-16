"""T-EM.16 — _QueryEmbedder dynamic field selection (B50).

Contract:
- Registry mode: `_QueryEmbedder(registry=, embed_callable=)` reads
  `registry.read_model()` and embeds the query with that one model.
  Output includes `embedding_field=model.field` so the downstream
  retriever can target the matching ES dense_vector field instead of
  the legacy hardcoded `embedding`.
- Legacy mode: `_QueryEmbedder(embedder)` (single-client) preserved for
  back-compat with the pre-T-EM ingest tests.
- IDLE: read_model returns stable → field = stable.field.
- CUTOVER: read_model returns candidate → field = candidate.field.
"""

from unittest.mock import MagicMock


def _model(name: str, dim: int) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.dim = dim
    m.field = f"embedding_{name.replace('-', '')}_{dim}"
    return m


def _registry(read: MagicMock) -> MagicMock:
    reg = MagicMock()
    reg.read_model.return_value = read
    return reg


# ---------------------------------------------------------------------------
# Registry mode
# ---------------------------------------------------------------------------


def test_registry_mode_embeds_with_read_model_and_emits_field_name() -> None:
    from ragent.pipelines.chat import _QueryEmbedder

    stable = _model("bge-m3", 1024)
    calls: list[tuple[str, list[str]]] = []

    def embed_callable(model, texts):
        calls.append((model.name, list(texts)))
        return [[0.5] * model.dim]

    qe = _QueryEmbedder(registry=_registry(stable), embed_callable=embed_callable)
    result = qe.run(query="what is rag?")

    assert result["query"] == "what is rag?"
    assert result["query_embedding"] == [0.5] * 1024
    assert result["embedding_field"] == "embedding_bgem3_1024"
    assert calls == [("bge-m3", ["what is rag?"])]


def test_registry_mode_picks_candidate_field_in_cutover_state() -> None:
    """When `embedding.read = "candidate"`, the registry's read_model
    returns the candidate; the query path embeds with it and points the
    retriever at `embedding_<cand>_<dim>`."""
    from ragent.pipelines.chat import _QueryEmbedder

    candidate = _model("bge-m3-v2", 768)

    def embed_callable(model, texts):
        return [[1.0] * model.dim]

    qe = _QueryEmbedder(registry=_registry(candidate), embed_callable=embed_callable)
    result = qe.run(query="hello")

    assert result["query_embedding"] == [1.0] * 768
    assert result["embedding_field"] == "embedding_bgem3v2_768"


def test_registry_mode_refreshes_read_model_per_call() -> None:
    """A live cutover during a long-running App must take effect on the
    next query, not require a restart. `read_model()` is therefore called
    fresh on every `run()`."""
    from ragent.pipelines.chat import _QueryEmbedder

    stable = _model("bge-m3", 1024)
    candidate = _model("bge-m3-v2", 768)
    reg = MagicMock()
    reg.read_model.side_effect = [stable, candidate]

    def embed_callable(model, texts):
        return [[1.0] * model.dim]

    qe = _QueryEmbedder(registry=reg, embed_callable=embed_callable)
    first = qe.run(query="q1")
    second = qe.run(query="q2")

    assert first["embedding_field"] == "embedding_bgem3_1024"
    assert second["embedding_field"] == "embedding_bgem3v2_768"
    assert reg.read_model.call_count == 2


# ---------------------------------------------------------------------------
# Legacy mode (back-compat)
# ---------------------------------------------------------------------------


def test_legacy_mode_preserves_pre_em_signature() -> None:
    """The (`embedder`)-only constructor must continue to work for the
    existing single-model integration tests during the registry rollout."""
    from ragent.pipelines.chat import _QueryEmbedder

    client = MagicMock()
    client.embed.return_value = [[0.25] * 1024]

    qe = _QueryEmbedder(client)
    result = qe.run(query="legacy")

    assert result["query"] == "legacy"
    assert result["query_embedding"] == [0.25] * 1024
    client.embed.assert_called_once_with(["legacy"], query=True)


def test_legacy_mode_does_not_emit_embedding_field() -> None:
    """Legacy mode does not know about field names — that's the registry's
    job. Output stays at the original two-key shape."""
    from ragent.pipelines.chat import _QueryEmbedder

    client = MagicMock()
    client.embed.return_value = [[0.0] * 1024]

    qe = _QueryEmbedder(client)
    result = qe.run(query="x")

    assert "embedding_field" not in result


# ---------------------------------------------------------------------------
# _DynamicFieldEmbeddingRetriever — targets a runtime-provided ES field
# ---------------------------------------------------------------------------


def test_dynamic_retriever_builds_knn_body_with_provided_field() -> None:
    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    store = MagicMock()
    store._search_documents.return_value = [MagicMock()]

    retriever = _DynamicFieldEmbeddingRetriever(document_store=store, top_k=10)
    retriever.run(query_embedding=[0.1] * 1024, embedding_field="embedding_bgem3v2_768")

    # _search_documents called once with a knn body whose `field` matches.
    store._search_documents.assert_called_once()
    body = store._search_documents.call_args.kwargs
    assert body["knn"]["field"] == "embedding_bgem3v2_768"
    assert body["knn"]["query_vector"] == [0.1] * 1024
    assert body["knn"]["k"] == 10


def test_dynamic_retriever_defaults_field_to_legacy_embedding_when_omitted() -> None:
    """When the upstream component doesn't emit `embedding_field` (legacy
    mode, or older pipelines), fall back to the historical `embedding` field
    so retrieval keeps working during the rollout."""
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
        embedding_field="embedding_bgem3_1024",
        top_k=25,
        filters={"field": "source_app", "operator": "==", "value": "confluence"},
    )

    body = store._search_documents.call_args.kwargs
    assert body["knn"]["k"] == 25
    # Filter is forwarded (Haystack-format dict; normalisation handled by ES store).
    assert "filter" in body["knn"]


def test_dynamic_retriever_rejects_empty_embedding() -> None:
    """Match the upstream contract — a zero-length vector is a bug."""
    import pytest

    from ragent.pipelines.chat import _DynamicFieldEmbeddingRetriever

    retriever = _DynamicFieldEmbeddingRetriever(document_store=MagicMock(), top_k=10)
    with pytest.raises(ValueError, match="non-empty"):
        retriever.run(query_embedding=[], embedding_field="embedding_bgem3_1024")
