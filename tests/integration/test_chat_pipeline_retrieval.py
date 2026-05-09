"""T3.5 — Chat retrieval pipeline: QueryEmbed→ES{Vector+BM25}→Join→Hydrate (B26, B29)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from haystack.dataclasses import Document

pytestmark = pytest.mark.docker

_EMBEDDING_DIM = 1024
_FIXED_EMBEDDING = [0.1] * _EMBEDDING_DIM


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


def _pipeline(es_store, mock_embedder, doc_repo, join_mode="rrf"):
    from ragent.pipelines.chat import build_retrieval_pipeline

    return build_retrieval_pipeline(
        embedder=mock_embedder,
        document_store=es_store,
        doc_repo=doc_repo,
        join_mode=join_mode,
    )


def _run(pipeline, query: str, filters: dict | None = None) -> list[Document]:
    """Run retrieval pipeline through the anyio bridge; returns hydrated documents."""
    from ragent.pipelines.chat import run_retrieval
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
