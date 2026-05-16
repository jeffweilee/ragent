"""T-EM.14 — DocumentEmbedder dual-write (B50).

Contract:
- IDLE state: `registry.write_models()` returns `[stable]`; embedder writes
  the stable model's vector to `doc.embedding` (legacy chunks_v1 field).
- CANDIDATE/CUTOVER state: `write_models()` returns `[stable, candidate]`;
  stable vector still lands on `doc.embedding`, candidate vector lands on
  `doc.meta[candidate.field]` (Haystack ES writer expands meta keys to
  top-level ES fields, so the chunk doc ends up with both
  `embedding` AND `embedding_<cand>_<dim>` populated).
- Embed callable is called once per model (one batch per model), not once
  per text. Stable always called first to preserve `doc.embedding`.
- Empty document list short-circuits — no embed calls.
"""

from unittest.mock import MagicMock

from haystack.dataclasses import Document

from ragent.clients.embedding_model_config import EmbeddingModelConfig


def _model(name: str, dim: int) -> EmbeddingModelConfig:
    """Build a real EmbeddingModelConfig — keeps the `.field` formula in
    sync with `_normalize` instead of reimplementing it as `replace('-','')`,
    which silently diverges on mixed-case names (e.g. `BGE-M3-v2`)."""
    return EmbeddingModelConfig(name=name, dim=dim, api_url="http://test", model_arg=name)


def _registry(*models: EmbeddingModelConfig) -> MagicMock:
    reg = MagicMock()
    reg.write_models.return_value = list(models)
    return reg


# ---------------------------------------------------------------------------
# IDLE state — single model
# ---------------------------------------------------------------------------


def test_idle_state_writes_stable_vector_to_doc_embedding() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    calls: list[tuple[str, list[str]]] = []

    def embed_callable(model, texts):
        calls.append((model.name, list(texts)))
        return [[0.1] * model.dim for _ in texts]

    embedder = DocumentEmbedder(registry=_registry(stable), embed_callable=embed_callable)
    docs = [Document(content="hello"), Document(content="world")]

    out = embedder.run(docs)["documents"]

    assert len(out) == 2
    assert out[0].embedding == [0.1] * 1024
    assert out[1].embedding == [0.1] * 1024
    # No candidate field in IDLE.
    assert "embedding_bgem3v2_768" not in (out[0].meta or {})
    # Embed called exactly once with the stable model and both texts.
    assert calls == [("bge-m3", ["hello", "world"])]


# ---------------------------------------------------------------------------
# CANDIDATE / CUTOVER state — dual-write
# ---------------------------------------------------------------------------


def test_dual_write_state_writes_stable_to_embedding_and_candidate_to_meta() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    candidate = _model("bge-m3-v2", 768)
    calls: list[str] = []

    def embed_callable(model, texts):
        calls.append(model.name)
        return [[float(model.dim)] * model.dim for _ in texts]

    embedder = DocumentEmbedder(
        registry=_registry(stable, candidate), embed_callable=embed_callable
    )
    docs = [Document(content="hello")]

    out = embedder.run(docs)["documents"]

    # Stable vector lands on the legacy `doc.embedding` field.
    assert out[0].embedding == [1024.0] * 1024
    # Candidate vector lands on `doc.meta[candidate.field]`.
    assert out[0].meta[candidate.field] == [768.0] * 768
    # Both models invoked, stable first.
    assert calls == ["bge-m3", "bge-m3-v2"]


def test_dual_write_preserves_existing_meta() -> None:
    """The candidate-field injection must not clobber chunk metadata
    that earlier pipeline stages (loader, splitter, chunker) attached."""
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    candidate = _model("bge-m3-v2", 768)

    def embed_callable(model, texts):
        return [[1.0] * model.dim for _ in texts]

    embedder = DocumentEmbedder(
        registry=_registry(stable, candidate), embed_callable=embed_callable
    )
    docs = [
        Document(
            content="hello",
            meta={"document_id": "DOCID-1", "source_app": "confluence"},
        )
    ]

    out = embedder.run(docs)["documents"]

    assert out[0].meta["document_id"] == "DOCID-1"
    assert out[0].meta["source_app"] == "confluence"
    assert out[0].meta[candidate.field] == [1.0] * 768


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_documents_short_circuit_skips_embed_calls() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    calls: list[str] = []

    def embed_callable(model, texts):
        calls.append(model.name)
        return []

    embedder = DocumentEmbedder(registry=_registry(stable), embed_callable=embed_callable)
    out = embedder.run([])["documents"]

    assert out == []
    assert calls == []


def test_each_model_invoked_once_per_run_not_once_per_text() -> None:
    """Batching invariant: many texts → one embed call per model."""
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    candidate = _model("bge-m3-v2", 768)
    invocations: list[tuple[str, int]] = []

    def embed_callable(model, texts):
        invocations.append((model.name, len(texts)))
        return [[1.0] * model.dim for _ in texts]

    embedder = DocumentEmbedder(
        registry=_registry(stable, candidate), embed_callable=embed_callable
    )
    docs = [Document(content=f"t{i}") for i in range(5)]

    embedder.run(docs)

    assert invocations == [("bge-m3", 5), ("bge-m3-v2", 5)]


def test_registry_with_no_write_models_raises() -> None:
    """An empty `write_models()` is a registry-state bug; refuse to write
    chunks with no embedding rather than silently produce field-less docs."""
    import pytest

    from ragent.pipelines.factory import DocumentEmbedder

    reg = MagicMock()
    reg.write_models.return_value = []
    embedder = DocumentEmbedder(registry=reg, embed_callable=lambda m, t: [])

    with pytest.raises(RuntimeError, match="no write_models"):
        embedder.run([Document(content="x")])
