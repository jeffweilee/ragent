"""T3.6 — Chat retrieval pipeline factory: QueryEmbed→ES{Vector+BM25}→Join→Hydrate (C6, B26)."""

from __future__ import annotations

import os
from typing import Any

from haystack.components.joiners import DocumentJoiner
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document
from haystack_integrations.components.retrievers.elasticsearch import (
    ElasticsearchBM25Retriever,
    ElasticsearchEmbeddingRetriever,
)

_EXCERPT_MAX_CHARS = int(os.environ.get("EXCERPT_MAX_CHARS", "512"))
_VALID_MODES = frozenset({"rrf", "concatenate", "vector_only", "bm25_only"})
_HAYSTACK_JOIN_MODE = {"rrf": "reciprocal_rank_fusion", "concatenate": "concatenate"}


@component
class _QueryEmbedder:
    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder

    @component.output_types(query=str, query_embedding=list[float])
    def run(self, query: str) -> dict:
        embedding = self._embedder.embed([query], query=True)[0]
        return {"query": query, "query_embedding": embedding}


@component
class _SourceHydrator:
    def __init__(self, doc_repo: Any) -> None:
        self._repo = doc_repo

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        ids = [d.meta.get("document_id") for d in documents if d.meta.get("document_id")]
        sources = self._repo.get_sources_by_document_ids(ids) if ids else {}
        for doc in documents:
            doc_id = doc.meta.get("document_id")
            if doc_id and doc_id in sources:
                source_app, source_id, source_title = sources[doc_id]
                doc.meta.update(
                    {"source_app": source_app, "source_id": source_id, "source_title": source_title}
                )
            if doc.content and len(doc.content) > _EXCERPT_MAX_CHARS:
                doc.content = doc.content[:_EXCERPT_MAX_CHARS]
        return {"documents": documents}


def build_retrieval_pipeline(
    embedder: Any,
    document_store: Any,
    doc_repo: Any,
    join_mode: str = "rrf",
) -> Pipeline:
    if join_mode not in _VALID_MODES:
        raise ValueError(f"join_mode must be one of {sorted(_VALID_MODES)}, got {join_mode!r}")

    pipeline = Pipeline()
    pipeline.add_component("source_hydrator", _SourceHydrator(doc_repo))

    if join_mode == "vector_only":
        pipeline.add_component("query_embedder", _QueryEmbedder(embedder))
        pipeline.add_component(
            "vector_retriever", ElasticsearchEmbeddingRetriever(document_store=document_store)
        )
        pipeline.connect("query_embedder.query_embedding", "vector_retriever.query_embedding")
        pipeline.connect("vector_retriever.documents", "source_hydrator.documents")

    elif join_mode == "bm25_only":
        pipeline.add_component(
            "bm25_retriever", ElasticsearchBM25Retriever(document_store=document_store)
        )
        pipeline.connect("bm25_retriever.documents", "source_hydrator.documents")

    else:  # rrf or concatenate
        pipeline.add_component("query_embedder", _QueryEmbedder(embedder))
        pipeline.add_component(
            "vector_retriever", ElasticsearchEmbeddingRetriever(document_store=document_store)
        )
        pipeline.add_component(
            "bm25_retriever", ElasticsearchBM25Retriever(document_store=document_store)
        )
        pipeline.add_component("joiner", DocumentJoiner(join_mode=_HAYSTACK_JOIN_MODE[join_mode]))
        pipeline.connect("query_embedder.query_embedding", "vector_retriever.query_embedding")
        pipeline.connect("vector_retriever.documents", "joiner.documents")
        pipeline.connect("bm25_retriever.documents", "joiner.documents")
        pipeline.connect("joiner.documents", "source_hydrator.documents")

    return pipeline


def run_retrieval(
    pipeline: Pipeline,
    query: str,
    filters: dict | None = None,
) -> list[Document]:
    """Run the retrieval pipeline; returns hydrated documents.

    Inspects which components are present and populates only the required inputs.
    """
    nodes = set(pipeline.graph.nodes)
    inputs: dict[str, dict] = {}

    if "query_embedder" in nodes:
        inputs["query_embedder"] = {"query": query}
    if "bm25_retriever" in nodes:
        bm25_input: dict[str, Any] = {"query": query}
        if filters:
            bm25_input["filters"] = filters
        inputs["bm25_retriever"] = bm25_input
    if "vector_retriever" in nodes and filters:
        inputs.setdefault("vector_retriever", {})["filters"] = filters

    result = pipeline.run(inputs)
    return result.get("source_hydrator", {}).get("documents", [])
