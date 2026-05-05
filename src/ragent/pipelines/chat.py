"""T3.6 — Chat retrieval pipeline factory: QueryEmbed→ES{Vector+BM25}→Join→Hydrate (C6, B26)."""

from __future__ import annotations

import dataclasses
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
_DEFAULT_TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "20"))
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
    """Enrich chunk metadata with source_app/source_id/source_title from MariaDB."""

    def __init__(self, doc_repo: Any) -> None:
        self._repo = doc_repo

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        ids = [d.meta.get("document_id") for d in documents if d.meta.get("document_id")]
        sources = self._repo.get_sources_by_document_ids(ids) if ids else {}
        result = []
        for doc in documents:
            doc_id = doc.meta.get("document_id")
            if doc_id and doc_id in sources:
                source_app, source_id, source_title = sources[doc_id]
                meta = {
                    **doc.meta,
                    "source_app": source_app,
                    "source_id": source_id,
                    "source_title": source_title,
                }
                result.append(dataclasses.replace(doc, meta=meta))
            else:
                result.append(doc)
        return {"documents": result}


@component
class _ExcerptTruncator:
    """Truncate chunk content to EXCERPT_MAX_CHARS for response payloads."""

    def __init__(self, max_chars: int = _EXCERPT_MAX_CHARS) -> None:
        self._max = max_chars

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        result = []
        for doc in documents:
            if doc.content and len(doc.content) > self._max:
                result.append(dataclasses.replace(doc, content=doc.content[: self._max]))
            else:
                result.append(doc)
        return {"documents": result}


def build_retrieval_pipeline(
    embedder: Any,
    document_store: Any,
    doc_repo: Any,
    join_mode: str = "rrf",
    top_k: int = _DEFAULT_TOP_K,
) -> Pipeline:
    if join_mode not in _VALID_MODES:
        raise ValueError(f"join_mode must be one of {sorted(_VALID_MODES)}, got {join_mode!r}")

    pipeline = Pipeline()
    pipeline.add_component("source_hydrator", _SourceHydrator(doc_repo))
    pipeline.add_component("excerpt_truncator", _ExcerptTruncator())
    pipeline.connect("source_hydrator.documents", "excerpt_truncator.documents")

    if join_mode == "vector_only":
        pipeline.add_component("query_embedder", _QueryEmbedder(embedder))
        pipeline.add_component(
            "vector_retriever",
            ElasticsearchEmbeddingRetriever(document_store=document_store, top_k=top_k),
        )
        pipeline.connect("query_embedder.query_embedding", "vector_retriever.query_embedding")
        pipeline.connect("vector_retriever.documents", "source_hydrator.documents")

    elif join_mode == "bm25_only":
        pipeline.add_component(
            "bm25_retriever",
            ElasticsearchBM25Retriever(document_store=document_store, top_k=top_k),
        )
        pipeline.connect("bm25_retriever.documents", "source_hydrator.documents")

    else:  # rrf or concatenate
        pipeline.add_component("query_embedder", _QueryEmbedder(embedder))
        pipeline.add_component(
            "vector_retriever",
            ElasticsearchEmbeddingRetriever(document_store=document_store, top_k=top_k),
        )
        pipeline.add_component(
            "bm25_retriever",
            ElasticsearchBM25Retriever(document_store=document_store, top_k=top_k),
        )
        pipeline.add_component(
            "joiner", DocumentJoiner(join_mode=_HAYSTACK_JOIN_MODE[join_mode], top_k=top_k)
        )
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
    return result.get("excerpt_truncator", {}).get("documents", [])
