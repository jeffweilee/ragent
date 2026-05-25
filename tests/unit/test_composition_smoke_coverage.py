"""T-B50 — production-wiring smoke coverage for registry-mode retrieval.

Ref: journal QA 2026-05-19 "Looks-like-prod, isn't-prod"; issue #119.

Scope: verify that ``build_retrieval_pipeline`` with the exact kwargs that
``bootstrap/composition.py`` passes (``registry`` + ``embed_query_callable``,
``rrf`` join) (a) builds a pipeline that uses ``_DynamicFieldEmbeddingRetriever``
and (b) can run a query end-to-end with mocked external dependencies.

This is the unit-tier complement to ``test_production_wiring_with_filter_smoke``
in ``tests/integration/test_chat_pipeline_retrieval.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from haystack.dataclasses import Document
from haystack_integrations.document_stores.elasticsearch import ElasticsearchDocumentStore

from ragent.clients.embedding_model_config import EmbeddingModelConfig
from ragent.pipelines.retrieve import (
    _DynamicFieldEmbeddingRetriever,
    build_retrieval_pipeline,
    run_retrieval,
)
from tests.conftest import run_in_threadpool

_EMBEDDING_DIM = 1024
_FIXED_EMBEDDING = [0.1] * _EMBEDDING_DIM
_REGISTRY_MODEL_NAME = "smokemodel"


def _stub_registry() -> MagicMock:
    """Return a registry stub with one model — mirrors the integration suite
    and the pattern used by composition.py at runtime."""
    model = EmbeddingModelConfig(
        name=_REGISTRY_MODEL_NAME,
        dim=_EMBEDDING_DIM,
        api_url="http://test",
        model_arg=_REGISTRY_MODEL_NAME,
    )
    reg = MagicMock()
    reg.read_model.return_value = model
    return reg


def _embed_query_callable(model: EmbeddingModelConfig, texts: list[str]) -> list[list[float]]:
    return [_FIXED_EMBEDDING] * len(texts)


def test_composition_registry_kwargs_selects_dynamic_retriever() -> None:
    """``build_retrieval_pipeline`` with the exact production registry kwargs
    (``registry`` + ``embed_query_callable``, ``join_mode="rrf"``) must
    instantiate ``_DynamicFieldEmbeddingRetriever`` for the vector slot.

    Regression gate: if ``composition.py`` ever accidentally falls back to the
    legacy branch (e.g. registry kwarg dropped), this test fails immediately
    rather than silently degrading B50 hot-swap semantics.
    """
    pipeline = build_retrieval_pipeline(
        document_store=MagicMock(spec=ElasticsearchDocumentStore),
        doc_repo=MagicMock(),
        join_mode="rrf",
        registry=_stub_registry(),
        embed_query_callable=_embed_query_callable,
    )

    assert "vector_retriever" in pipeline.graph.nodes
    vector_retriever = pipeline.get_component("vector_retriever")
    assert isinstance(vector_retriever, _DynamicFieldEmbeddingRetriever), (
        "production-wiring (registry mode) must use _DynamicFieldEmbeddingRetriever, "
        "not the legacy ElasticsearchEmbeddingRetriever"
    )


def test_composition_smoke_registry_end_to_end() -> None:
    """Build with the production registry kwargs and run one query end-to-end
    against a mocked document store.

    Uses ``vector_only`` join mode to avoid requiring a BM25 mock alongside
    the kNN mock; the intent is pipeline-graph execution coverage, not join
    semantics (those are covered by integration tests with real ES).

    Unit-tier complement to ``test_production_wiring_with_filter_smoke``;
    exercises the full component chain (query_embedder → vector_retriever →
    source_hydrator → excerpt_truncator) without a live ES cluster.
    """
    _CHUNK_ID = "smoke-unit-1"
    _DOC_ID = "doc-smoke-unit-1"
    _SOURCE_APP = "smoke_unit_app"

    fake_doc = Document(
        id=_CHUNK_ID,
        content="production wiring smoke unit test",
        meta={
            "chunk_id": _CHUNK_ID,
            "document_id": _DOC_ID,
            "source_app": _SOURCE_APP,
        },
    )

    mock_store = MagicMock(spec=ElasticsearchDocumentStore)
    mock_store._search_documents.return_value = [fake_doc]

    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        _DOC_ID: (_SOURCE_APP, "src-smoke-unit-1", "Smoke Unit Title"),
    }

    pipeline = build_retrieval_pipeline(
        document_store=mock_store,
        doc_repo=doc_repo,
        join_mode="vector_only",
        registry=_stub_registry(),
        embed_query_callable=_embed_query_callable,
    )

    docs = run_in_threadpool(lambda: run_retrieval(pipeline, query="production wiring smoke"))

    assert len(docs) == 1
    assert docs[0].meta["source_app"] == _SOURCE_APP
    assert docs[0].meta["source_title"] == "Smoke Unit Title"
