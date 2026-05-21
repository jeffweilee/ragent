"""T3.5 — Chat retrieval pipeline: QueryEmbed→ES{Vector+BM25}→Join→Hydrate (B26, B29)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from haystack.dataclasses import Document

pytestmark = pytest.mark.docker

_EMBEDDING_DIM = 1024
_FIXED_EMBEDDING = [0.1] * _EMBEDDING_DIM

# B50 registry-mode field for the parametrized filter test. Production wires
# `_DynamicFieldEmbeddingRetriever` against a per-model dense_vector field
# computed from `EmbeddingModelConfig.field`; the fixture installs the
# matching mapping so registry-mode kNN has a field to query.
_REGISTRY_MODEL_NAME = "testmodel"
_REGISTRY_MODEL_FIELD = f"embedding_{_REGISTRY_MODEL_NAME}_{_EMBEDDING_DIM}"


@pytest.fixture(scope="module")
def es_store(es_url: str):
    import json
    import urllib.request

    from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

    from ragent.bootstrap.init_schema import init_es

    init_es(es_url)
    # Wait for the freshly-created index shards to be allocated before searching.
    with urllib.request.urlopen(
        f"{es_url}/_cluster/health/chunks_v1?wait_for_status=yellow&timeout=60s", timeout=70
    ) as resp:
        health = json.loads(resp.read())
        if health.get("status") not in ("yellow", "green"):
            raise RuntimeError(f"chunks_v1 index not ready: {health}")
    # Install the registry-mode dense_vector field used by the parametrized
    # filter test. Mirrors what EmbeddingLifecycleService.promote() does in
    # production when a new model is added.
    put_mapping = urllib.request.Request(
        f"{es_url}/chunks_v1/_mapping",
        method="PUT",
        data=json.dumps(
            {
                "properties": {
                    _REGISTRY_MODEL_FIELD: {
                        "type": "dense_vector",
                        "dims": _EMBEDDING_DIM,
                        "index": True,
                        "similarity": "cosine",
                        "index_options": {"type": "flat"},
                    }
                }
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(put_mapping, timeout=30) as resp:
        resp.read()
    store = ElasticsearchDocumentStore(
        hosts=es_url,
        index="chunks_v1",
        embedding_similarity_function="cosine",
    )
    # Other test modules in the suite may have populated chunks_v1. Empty it
    # before this module's first test so test_empty_index_returns_no_documents
    # actually sees zero documents.
    delete_req = urllib.request.Request(
        f"{es_url}/chunks_v1/_delete_by_query?refresh=true",
        method="POST",
        data=b'{"query":{"match_all":{}}}',
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(delete_req, timeout=30) as resp:
        resp.read()
    return store


@pytest.fixture(scope="module")
def mock_embedder():
    embedder = MagicMock()
    embedder.embed.return_value = [_FIXED_EMBEDDING]
    return embedder


def _stub_registry():
    """Registry stub for the B50 registry-mode pipeline branch — returns a
    single model whose `.field` matches the dense_vector field installed by
    the `es_store` fixture so kNN has a real field to target."""
    from ragent.clients.embedding_model_config import EmbeddingModelConfig

    model = EmbeddingModelConfig(
        name=_REGISTRY_MODEL_NAME,
        dim=_EMBEDDING_DIM,
        api_url="http://test",
        model_arg=_REGISTRY_MODEL_NAME,
    )
    reg = MagicMock()
    reg.read_model.return_value = model
    return reg


def _embed_query_callable(model, texts):
    return [_FIXED_EMBEDDING] * len(texts)


def _pipeline(es_store, mock_embedder, doc_repo, *, mode="legacy", join_mode="rrf"):
    """Build the retrieval pipeline. `mode="legacy"` instantiates Haystack's
    standard `ElasticsearchEmbeddingRetriever`; `mode="registry"` matches the
    production composition root (`bootstrap/composition.py`) and instantiates
    `_DynamicFieldEmbeddingRetriever` — distinct filter path, must be
    integration-tested separately or it ships with zero ES coverage."""
    from ragent.pipelines.retrieve import build_retrieval_pipeline

    if mode == "registry":
        return build_retrieval_pipeline(
            document_store=es_store,
            doc_repo=doc_repo,
            join_mode=join_mode,
            registry=_stub_registry(),
            embed_query_callable=_embed_query_callable,
        )
    return build_retrieval_pipeline(
        embedder=mock_embedder,
        document_store=es_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
    )


def _run(pipeline, query: str, filters: dict | None = None) -> list[Document]:
    """Run retrieval pipeline through the anyio bridge; returns hydrated documents."""
    from ragent.pipelines.retrieve import run_retrieval
    from tests.conftest import run_in_threadpool

    return run_in_threadpool(lambda: run_retrieval(pipeline, query=query, filters=filters))


def _write_and_refresh(es_store, docs: list[Document]) -> None:
    es_store.write_documents(docs)
    # Allow ES to refresh its index so documents are immediately searchable.
    time.sleep(1)


# ── empty index ──────────────────────────────────────────────────────────────


def test_empty_index_returns_no_documents(es_store, mock_embedder) -> None:
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {}

    pipeline = _pipeline(es_store, mock_embedder, doc_repo)
    docs = _run(pipeline, "anything that should not match")
    assert docs == []


# ── BM25 retrieval ───────────────────────────────────────────────────────────


def test_bm25_retrieves_matching_document(es_store, mock_embedder) -> None:
    _write_and_refresh(
        es_store,
        [
            Document(
                id="bm25-doc-1",
                content="gradient descent optimizer converges faster",
                meta={
                    "chunk_id": "bm25-doc-1",
                    "document_id": "doc-bm25",
                    "source_app": "app_bm25",
                },
                embedding=_FIXED_EMBEDDING,
            )
        ],
    )
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-bm25": ("app_bm25", "src-bm25", "BM25 Title")
    }

    pipeline = _pipeline(es_store, mock_embedder, doc_repo, join_mode="bm25_only")
    docs = _run(pipeline, "gradient descent")

    assert any("gradient descent" in (d.content or "") for d in docs), (
        "BM25 should recall the document by matching text"
    )


# ── vector retrieval ─────────────────────────────────────────────────────────


def test_vector_retrieves_document_by_embedding(es_store, mock_embedder) -> None:
    _write_and_refresh(
        es_store,
        [
            Document(
                id="vec-doc-1",
                content="vector similarity search demo",
                meta={
                    "chunk_id": "vec-doc-1",
                    "document_id": "doc-vec",
                    "source_app": "app_vec",
                },
                embedding=_FIXED_EMBEDDING,
            )
        ],
    )
    doc_repo = AsyncMock()
    # Hydrator drops chunks whose document_id is not in the READY set (B36).
    # This test exercises retrieval, so the candidate doc must be hydratable.
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-vec": ("app_vec", "src-vec", "Vector Title"),
    }

    pipeline = _pipeline(es_store, mock_embedder, doc_repo, join_mode="vector_only")
    docs = _run(pipeline, "vector similarity")

    assert len(docs) >= 1, "vector kNN should recall the document with matching embedding"


# ── source hydration ──────────────────────────────────────────────────────────


def test_source_hydrator_enriches_documents(es_store, mock_embedder) -> None:
    _write_and_refresh(
        es_store,
        [
            Document(
                id="hydrate-doc-1",
                content="hydration enrichment test",
                meta={
                    "chunk_id": "hydrate-doc-1",
                    "document_id": "doc-hydrate",
                    "source_app": "app_h",
                },
                embedding=_FIXED_EMBEDDING,
            )
        ],
    )
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-hydrate": ("app_h", "src-xyz", "Hydrated Title")
    }

    pipeline = _pipeline(es_store, mock_embedder, doc_repo, join_mode="vector_only")
    docs = _run(pipeline, "hydration enrichment")

    hydrated = [d for d in docs if d.meta.get("document_id") == "doc-hydrate"]
    assert len(hydrated) >= 1
    assert hydrated[0].meta["source_id"] == "src-xyz"
    assert hydrated[0].meta["source_title"] == "Hydrated Title"


# ── excerpt truncation ────────────────────────────────────────────────────────


def test_pipeline_returns_full_content_untruncated(es_store, mock_embedder) -> None:
    # Truncation was moved to the router layer (_build_sources / _to_chunk).
    # The pipeline itself must return full chunk content so the LLM sees everything.
    long_text = "x" * 200
    _write_and_refresh(
        es_store,
        [
            Document(
                id="trunc-doc-1",
                content=long_text,
                meta={
                    "chunk_id": "trunc-doc-1",
                    "document_id": "doc-trunc",
                    "source_app": "app_t",
                },
                embedding=_FIXED_EMBEDDING,
            )
        ],
    )
    doc_repo = AsyncMock()
    # Hydrator drops on miss (B36); this test asserts truncation, not drop.
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-trunc": ("app_t", "src-trunc", "Trunc Title"),
    }

    pipeline = _pipeline(es_store, mock_embedder, doc_repo, join_mode="vector_only")
    docs = _run(pipeline, "x" * 10)

    assert any(len(d.content or "") == 200 for d in docs if d.content)


# ── source_app filter ────────────────────────────────────────────────────────


def test_filter_source_app_isolates_results(es_store, mock_embedder) -> None:
    _write_and_refresh(
        es_store,
        [
            Document(
                id="filter-alpha-1",
                content="filter test document alpha tenant",
                meta={
                    "chunk_id": "filter-alpha-1",
                    "document_id": "doc-alpha",
                    "source_app": "alpha_app",
                },
                embedding=_FIXED_EMBEDDING,
            ),
            Document(
                id="filter-beta-1",
                content="filter test document beta tenant",
                meta={
                    "chunk_id": "filter-beta-1",
                    "document_id": "doc-beta",
                    "source_app": "beta_app",
                },
                embedding=_FIXED_EMBEDDING,
            ),
        ],
    )
    doc_repo = AsyncMock()
    # Hydrator drops on miss (B36); this test asserts ES `term` filter, not drop.
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-alpha": ("alpha_app", "src-alpha", "Alpha"),
        "doc-beta": ("beta_app", "src-beta", "Beta"),
    }

    pipeline = _pipeline(es_store, mock_embedder, doc_repo, join_mode="bm25_only")
    docs = _run(
        pipeline,
        "filter test document",
        filters={"field": "source_app", "operator": "==", "value": "alpha_app"},
    )

    assert len(docs) >= 1
    assert all(d.meta.get("source_app") == "alpha_app" for d in docs), (
        "filter should exclude beta_app documents"
    )


# ── source_app filter — vector retriever path (legacy vs registry) ───────────


@pytest.mark.parametrize("mode", ["legacy", "registry"])
def test_filter_source_app_vector_path(es_store, mock_embedder, mode: str) -> None:
    """Pin the vector-retriever filter path against real ES for BOTH pipeline
    construction modes. `legacy` exercises Haystack's
    `ElasticsearchEmbeddingRetriever` (which normalises filters internally);
    `registry` exercises `_DynamicFieldEmbeddingRetriever` — the B50 branch
    that production wires via `composition.py`. Until this test, only the
    legacy retriever's filter shape was checked end-to-end, so a malformed
    filter on the registry path shipped silently (chat/retrieve 400)."""
    # Mode-suffix the scope values too — the module-scoped `es_store` fixture
    # persists docs across parametrize iterations, so reusing the same
    # source_app across [legacy] and [registry] would let the second run see
    # the first's chunk and break the exact-count assertion below.
    alpha_doc_id = f"doc-vfilter-alpha-{mode}"
    beta_doc_id = f"doc-vfilter-beta-{mode}"
    alpha_app = f"vfilter_alpha_app_{mode}"
    beta_app = f"vfilter_beta_app_{mode}"
    _write_and_refresh(
        es_store,
        [
            Document(
                id=f"vfilter-alpha-1-{mode}",
                content="vector filter test document alpha tenant",
                meta={
                    "chunk_id": f"vfilter-alpha-1-{mode}",
                    "document_id": alpha_doc_id,
                    "source_app": alpha_app,
                    _REGISTRY_MODEL_FIELD: _FIXED_EMBEDDING,
                },
                embedding=_FIXED_EMBEDDING,
            ),
            Document(
                id=f"vfilter-beta-1-{mode}",
                content="vector filter test document beta tenant",
                meta={
                    "chunk_id": f"vfilter-beta-1-{mode}",
                    "document_id": beta_doc_id,
                    "source_app": beta_app,
                    _REGISTRY_MODEL_FIELD: _FIXED_EMBEDDING,
                },
                embedding=_FIXED_EMBEDDING,
            ),
        ],
    )
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        alpha_doc_id: (alpha_app, "src-vfilter-alpha", "Alpha"),
        beta_doc_id: (beta_app, "src-vfilter-beta", "Beta"),
    }

    pipeline = _pipeline(es_store, mock_embedder, doc_repo, mode=mode, join_mode="vector_only")
    docs = _run(
        pipeline,
        "vector filter test document",
        filters={"field": "source_app", "operator": "==", "value": alpha_app},
    )

    assert len(docs) == 1, (
        f"vector retriever in {mode} mode: expected exactly 1 doc, got {len(docs)}"
    )
    assert docs[0].meta.get("source_app") == alpha_app


def test_production_wiring_with_filter_smoke(es_store) -> None:
    """Build the retrieval pipeline with the exact constructor shape used by
    `bootstrap/composition.py` (registry + embed_query_callable + rrf join)
    and run one filtered query against real ES. Regression coverage for the
    chat/retrieve 400 filter-malformed bug — pins the production wiring so a
    future filter-path change cannot ship without ES-level verification."""
    _write_and_refresh(
        es_store,
        [
            Document(
                id="prod-wire-1",
                content="production wiring smoke test alpha",
                meta={
                    "chunk_id": "prod-wire-1",
                    "document_id": "doc-prod-wire-1",
                    "source_app": "prod_wire_app",
                    "source_meta": "prod_wire_space",
                    _REGISTRY_MODEL_FIELD: _FIXED_EMBEDDING,
                },
                embedding=_FIXED_EMBEDDING,
            ),
            Document(
                id="prod-wire-2",
                content="production wiring smoke test bravo",
                meta={
                    "chunk_id": "prod-wire-2",
                    "document_id": "doc-prod-wire-2",
                    "source_app": "other_app",
                    "source_meta": "other_space",
                    _REGISTRY_MODEL_FIELD: _FIXED_EMBEDDING,
                },
                embedding=_FIXED_EMBEDDING,
            ),
        ],
    )
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-prod-wire-1": ("prod_wire_app", "src-prod-wire-1", "Prod Wire 1"),
        "doc-prod-wire-2": ("other_app", "src-prod-wire-2", "Prod Wire 2"),
    }

    from ragent.pipelines.retrieve import build_es_filters, build_retrieval_pipeline

    pipeline = build_retrieval_pipeline(
        document_store=es_store,
        doc_repo=doc_repo,
        join_mode="rrf",
        registry=_stub_registry(),
        embed_query_callable=_embed_query_callable,
    )
    docs = _run(
        pipeline,
        "production wiring smoke test",
        filters=build_es_filters(source_app="prod_wire_app", source_meta="prod_wire_space"),
    )

    assert len(docs) == 1, f"production-wiring smoke: expected exactly 1 doc, got {len(docs)}"
    assert docs[0].meta.get("source_app") == "prod_wire_app"
    assert docs[0].meta.get("source_meta") == "prod_wire_space"
