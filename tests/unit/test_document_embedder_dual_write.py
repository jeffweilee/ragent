"""T-EM.14 / T-EM-R.6 — DocumentEmbedder dual-index write.

Contract:
- IDLE state: registry.write_models() returns [stable]; embedder bulk-writes
  to stable_index with field "embedding".
- CANDIDATE/CUTOVER state: write_models() returns [stable, candidate]; embedder
  bulk-writes to stable_index AND candidate_index, always field "embedding".
- documents output is always [] — the embedder is the sole ES writer.
- Empty document list short-circuits — no embed calls, no bulk calls.
"""

from unittest.mock import MagicMock

import pytest
from haystack.dataclasses import Document

from ragent.clients.embedding_model_config import EmbeddingModelConfig


def _model(name: str, dim: int) -> EmbeddingModelConfig:
    return EmbeddingModelConfig(name=name, dim=dim, api_url="http://test", model_arg=name)


def _registry(
    *models: EmbeddingModelConfig,
    stable_index: str = "chunks_v1",
    candidate_index: str | None = None,
) -> MagicMock:
    reg = MagicMock()
    reg.write_models.return_value = list(models)
    reg.stable_index = stable_index
    reg.candidate_index = candidate_index
    return reg


def _es() -> MagicMock:
    es = MagicMock()
    es.bulk.return_value = {"errors": False, "items": []}
    return es


# ---------------------------------------------------------------------------
# IDLE state — single model → stable_index only
# ---------------------------------------------------------------------------


def test_idle_writes_to_stable_index_via_bulk() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    es = _es()

    def embed_fn(model, texts):
        return [[0.1] * model.dim for _ in texts]

    embedder = DocumentEmbedder(
        registry=_registry(stable, stable_index="chunks_v1"),
        embed_callable=embed_fn,
        es_client=es,
    )
    docs = [Document(content="hello"), Document(content="world")]

    out = embedder.run(docs)["documents"]

    assert out == []
    es.bulk.assert_called_once()
    call_kwargs = es.bulk.call_args.kwargs
    assert call_kwargs["index"] == "chunks_v1"


def test_idle_bulk_body_uses_embedding_field_not_model_field() -> None:
    """T-EM-R.6 — bulk body uses 'embedding' key, not embedding_{model}_{dim}."""
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    es = _es()

    embedder = DocumentEmbedder(
        registry=_registry(stable, stable_index="chunks_v1"),
        embed_callable=lambda m, texts: [[0.5] * m.dim for _ in texts],
        es_client=es,
    )
    embedder.run([Document(content="hi")])

    ops = es.bulk.call_args.kwargs["operations"]
    # Alternating: action dict, body dict, action dict, body dict ...
    bodies = [ops[i] for i in range(1, len(ops), 2)]
    assert all("embedding" in b for b in bodies)
    assert all(stable.field not in b for b in bodies), (
        "embedding_{model}_{dim} key must not appear in bulk body"
    )


# ---------------------------------------------------------------------------
# CANDIDATE / CUTOVER — dual write to both indices
# ---------------------------------------------------------------------------


def test_candidate_writes_to_stable_and_candidate_index() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    cand = _model("bge-m3-v2", 768)
    es = _es()

    embedder = DocumentEmbedder(
        registry=_registry(stable, cand, stable_index="chunks_v1", candidate_index="chunks_v2"),
        embed_callable=lambda m, texts: [[float(m.dim)] * m.dim for _ in texts],
        es_client=es,
    )
    out = embedder.run([Document(content="hello")])["documents"]

    assert out == []
    assert es.bulk.call_count == 2
    indices = {c.kwargs["index"] for c in es.bulk.call_args_list}
    assert indices == {"chunks_v1", "chunks_v2"}


def test_candidate_both_bulk_calls_use_embedding_field() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    cand = _model("bge-m3-v2", 768)
    es = _es()

    embedder = DocumentEmbedder(
        registry=_registry(stable, cand, stable_index="chunks_v1", candidate_index="chunks_v2"),
        embed_callable=lambda m, texts: [[1.0] * m.dim for _ in texts],
        es_client=es,
    )
    embedder.run([Document(content="hello")])

    for bulk_call in es.bulk.call_args_list:
        ops = bulk_call.kwargs["operations"]
        bodies = [ops[i] for i in range(1, len(ops), 2)]
        assert all("embedding" in b for b in bodies)
        assert all(stable.field not in b and cand.field not in b for b in bodies)


def test_bulk_action_uses_doc_id_as_es_id() -> None:
    """Each bulk action must set _id from doc.id for idempotent overwrite."""
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    es = _es()
    doc = Document(content="hello")

    embedder = DocumentEmbedder(
        registry=_registry(stable, stable_index="chunks_v1"),
        embed_callable=lambda m, texts: [[0.1] * m.dim for _ in texts],
        es_client=es,
    )
    embedder.run([doc])

    ops = es.bulk.call_args.kwargs["operations"]
    actions = [ops[i] for i in range(0, len(ops), 2)]
    assert all(a["index"]["_id"] == doc.id for a in actions)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_documents_skips_embed_and_bulk() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    calls: list[str] = []
    es = _es()

    embedder = DocumentEmbedder(
        registry=_registry(stable),
        embed_callable=lambda m, texts: calls.append(m.name) or [],
        es_client=es,
    )
    out = embedder.run([])["documents"]

    assert out == []
    assert calls == []
    es.bulk.assert_not_called()


def test_empty_write_models_raises() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    reg = MagicMock()
    reg.write_models.return_value = []
    embedder = DocumentEmbedder(
        registry=reg,
        embed_callable=lambda m, t: [],
        es_client=_es(),
    )

    with pytest.raises(RuntimeError, match="no write_models"):
        embedder.run([Document(content="x")])


def test_registry_mode_requires_es_client() -> None:
    from ragent.pipelines.factory import DocumentEmbedder

    with pytest.raises(ValueError, match="es_client"):
        DocumentEmbedder(
            registry=MagicMock(),
            embed_callable=lambda m, t: [],
            es_client=None,
        )


def test_two_models_with_no_candidate_index_raises() -> None:
    """write_models() returns 2 models but candidate_index is None — must fail loudly."""
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    cand = _model("bge-m3-v2", 768)
    embedder = DocumentEmbedder(
        registry=_registry(stable, cand, stable_index="chunks_v1", candidate_index=None),
        embed_callable=lambda m, texts: [[1.0] * m.dim for _ in texts],
        es_client=_es(),
    )

    with pytest.raises(RuntimeError, match="candidate_index is None"):
        embedder.run([Document(content="hello")])


def test_each_model_embedded_once_not_per_text() -> None:
    """Batching invariant: many texts → one embed call per model."""
    from ragent.pipelines.factory import DocumentEmbedder

    stable = _model("bge-m3", 1024)
    cand = _model("bge-m3-v2", 768)
    invocations: list[tuple[str, int]] = []

    def embed_fn(model, texts):
        invocations.append((model.name, len(texts)))
        return [[1.0] * model.dim for _ in texts]

    embedder = DocumentEmbedder(
        registry=_registry(stable, cand, candidate_index="chunks_v2"),
        embed_callable=embed_fn,
        es_client=_es(),
    )
    embedder.run([Document(content=f"t{i}") for i in range(5)])

    assert invocations == [("bge-m3", 5), ("bge-m3-v2", 5)]


# ---------------------------------------------------------------------------
# build_ingest_pipeline — no writer step
# ---------------------------------------------------------------------------


def test_build_ingest_pipeline_has_no_writer_component() -> None:
    """T-EM-R.6 — DocumentEmbedder is the sole ES writer; no DocumentWriter in pipeline."""
    from ragent.pipelines.factory import DocumentEmbedder, build_ingest_pipeline

    stable = _model("bge-m3", 1024)
    embedder = DocumentEmbedder(
        registry=_registry(stable),
        embed_callable=lambda m, texts: [[0.1] * m.dim for _ in texts],
        es_client=_es(),
    )
    pipeline = build_ingest_pipeline(embedder)

    assert "writer" not in pipeline.graph.nodes
