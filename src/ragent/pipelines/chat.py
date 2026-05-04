"""T3.6 — Chat retrieval pipeline factory: QueryEmbed→ES{Vector+BM25}→Join→Hydrate (C6, B26)."""

from __future__ import annotations

import os
from typing import Any

from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document

_EXCERPT_MAX_CHARS = int(os.environ.get("EXCERPT_MAX_CHARS", "512"))
_VALID_MODES = frozenset({"rrf", "concatenate", "vector_only", "bm25_only"})


@component
class _QueryEmbedder:
    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder

    @component.output_types(embedding=list[float])
    def run(self, query: str) -> dict:
        result = self._embedder.embed([query], query=True)
        return {"embedding": result[0]}


@component
class _ESVectorRetriever:
    def __init__(self, document_store: Any) -> None:
        self._store = document_store

    @component.output_types(documents=list[Document])
    def run(self, embedding: list[float], filters: dict | None = None) -> dict:
        return {"documents": []}


@component
class _ESBM25Retriever:
    def __init__(self, document_store: Any) -> None:
        self._store = document_store

    @component.output_types(documents=list[Document])
    def run(self, query: str, filters: dict | None = None) -> dict:
        return {"documents": []}


@component
class _DocumentJoiner:
    def __init__(self, join_mode: str = "rrf") -> None:
        self._mode = join_mode

    @component.output_types(documents=list[Document])
    def run(self, documents: list[list[Document]]) -> dict:
        merged = [doc for docs in documents for doc in docs]
        return {"documents": merged}


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
    pipeline.add_component("query_embedder", _QueryEmbedder(embedder))
    pipeline.add_component("source_hydrator", _SourceHydrator(doc_repo))

    if join_mode == "vector_only":
        pipeline.add_component("vector_retriever", _ESVectorRetriever(document_store))
        pipeline.connect("query_embedder.embedding", "vector_retriever.embedding")
        pipeline.connect("vector_retriever.documents", "source_hydrator.documents")

    elif join_mode == "bm25_only":
        pipeline.add_component("bm25_retriever", _ESBM25Retriever(document_store))
        pipeline.connect("bm25_retriever.documents", "source_hydrator.documents")

    else:  # rrf or concatenate
        pipeline.add_component("vector_retriever", _ESVectorRetriever(document_store))
        pipeline.add_component("bm25_retriever", _ESBM25Retriever(document_store))
        pipeline.add_component("joiner", _DocumentJoiner(join_mode))
        pipeline.connect("query_embedder.embedding", "vector_retriever.embedding")
        pipeline.connect("vector_retriever.documents", "joiner.documents")
        pipeline.connect("bm25_retriever.documents", "joiner.documents")
        pipeline.connect("joiner.documents", "source_hydrator.documents")

    return pipeline
