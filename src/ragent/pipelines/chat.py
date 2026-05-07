"""T3.6 — Chat retrieval pipeline factory: QueryEmbed→ES{Vector+BM25}→Join→Hydrate (C6, B26)."""

from __future__ import annotations

import dataclasses
from typing import Any

import anyio.from_thread
import structlog
from haystack.components.joiners import DocumentJoiner
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document
from haystack_integrations.components.retrievers.elasticsearch import (
    ElasticsearchBM25Retriever,
    ElasticsearchEmbeddingRetriever,
)

from ragent.utility.env import int_env

_EXCERPT_MAX_CHARS = int_env("EXCERPT_MAX_CHARS", 512)
_DEFAULT_TOP_K = int_env("RETRIEVAL_TOP_K", 20)
_VALID_MODES = frozenset({"rrf", "concatenate", "vector_only", "bm25_only"})

_logger = structlog.get_logger(__name__)


def build_es_filters(source_app: str | None, source_workspace: str | None) -> dict | None:
    clauses = []
    if source_app:
        clauses.append({"field": "source_app", "operator": "==", "value": source_app})
    if source_workspace:
        clauses.append({"field": "source_workspace", "operator": "==", "value": source_workspace})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"operator": "AND", "conditions": clauses}


def doc_to_source_entry(doc: Any) -> dict:
    meta = doc.meta or {}
    excerpt_src = meta.get("raw_content") or (doc.content or "")
    return {
        "document_id": meta.get("document_id"),
        "source_app": meta.get("source_app"),
        "source_id": meta.get("source_id"),
        "type": "knowledge",
        "source_title": meta.get("source_title"),
        "source_url": meta.get("source_url"),
        "mime_type": meta.get("mime_type") or meta.get("content_type"),
        "excerpt": excerpt_src[:_EXCERPT_MAX_CHARS],
    }


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
        sources = anyio.from_thread.run(self._repo.get_sources_by_document_ids, ids) if ids else {}
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
class _Reranker:
    """Wrap RerankClient as a Haystack component.

    Sits between the joiner (or single retriever) and source_hydrator so
    rerank scoring sees full chunk content, before excerpt truncation.
    """

    def __init__(self, rerank_client: Any, top_k: int = _DEFAULT_TOP_K) -> None:
        self._client = rerank_client
        self._top_k = top_k

    @component.output_types(documents=list[Document])
    def run(self, query: str, documents: list[Document]) -> dict:
        if not documents:
            return {"documents": []}
        texts = [d.content or "" for d in documents]
        results = self._client.rerank(query=query, texts=texts, top_k=self._top_k)
        ordered: list[Document] = []
        invalid = 0
        for r in results[: self._top_k]:
            i = r.get("index")
            # bool is an int subclass, so isinstance(True, int) is True; reject
            # explicitly so {"index": True} is not silently treated as docs[1].
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(documents):
                invalid += 1
                continue
            score = r.get("score")
            doc = documents[i]
            ordered.append(dataclasses.replace(doc, score=score) if score is not None else doc)
        if invalid:
            # Reranker returned indices outside the candidate set — surfaces
            # contract drift between retrieval top_k and rerank result_count.
            _logger.warning(
                "rerank.invalid_indices",
                invalid_count=invalid,
                result_count=len(results),
                document_count=len(documents),
            )
        return {"documents": ordered}


@component
class _LLMGenerator:
    """Wrap LLMClient.chat as a Haystack component.

    Terminal node for non-streaming chat: takes RAG-built messages plus
    cited documents, returns the answer string and passes documents
    through for citation rendering.
    """

    def __init__(self, llm_client: Any) -> None:
        self._client = llm_client

    @component.output_types(answer=str, documents=list[Document], usage=dict)
    def run(
        self,
        messages: list[dict],
        documents: list[Document],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> dict:
        result = self._client.chat(
            messages=messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
        return {"answer": result["content"], "documents": documents, "usage": result["usage"]}


@component
class _ExcerptTruncator:
    """Truncate chunk content to EXCERPT_MAX_CHARS for response payloads."""

    def __init__(self, max_chars: int = _EXCERPT_MAX_CHARS) -> None:
        self._max = max_chars

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        result = []
        for doc in documents:
            # Display layer prefers raw byte slice; fall back to normalized
            # content for legacy chunks predating raw_content.
            source = (doc.meta or {}).get("raw_content") or (doc.content or "")
            truncated = source[: self._max]
            if truncated != doc.content:
                result.append(dataclasses.replace(doc, content=truncated))
            else:
                result.append(doc)
        return {"documents": result}


def build_retrieval_pipeline(
    embedder: Any,
    document_store: Any,
    doc_repo: Any,
    join_mode: str = "rrf",
    top_k: int = _DEFAULT_TOP_K,
    rerank_client: Any | None = None,
) -> Pipeline:
    if join_mode not in _VALID_MODES:
        raise ValueError(f"join_mode must be one of {sorted(_VALID_MODES)}, got {join_mode!r}")

    pipeline = Pipeline()
    pipeline.add_component("source_hydrator", _SourceHydrator(doc_repo))
    pipeline.add_component("excerpt_truncator", _ExcerptTruncator())
    pipeline.connect("source_hydrator.documents", "excerpt_truncator.documents")

    # The retriever output feeds either reranker → source_hydrator (when a
    # rerank_client is configured) or source_hydrator directly.
    if rerank_client is not None:
        pipeline.add_component("reranker", _Reranker(rerank_client, top_k=top_k))
        pipeline.connect("reranker.documents", "source_hydrator.documents")
        retriever_sink = "reranker.documents"
    else:
        retriever_sink = "source_hydrator.documents"

    if join_mode == "vector_only":
        pipeline.add_component("query_embedder", _QueryEmbedder(embedder))
        pipeline.add_component(
            "vector_retriever",
            ElasticsearchEmbeddingRetriever(document_store=document_store, top_k=top_k),
        )
        pipeline.connect("query_embedder.query_embedding", "vector_retriever.query_embedding")
        pipeline.connect("vector_retriever.documents", retriever_sink)

    elif join_mode == "bm25_only":
        pipeline.add_component(
            "bm25_retriever",
            ElasticsearchBM25Retriever(document_store=document_store, top_k=top_k),
        )
        pipeline.connect("bm25_retriever.documents", retriever_sink)

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
        pipeline.connect("joiner.documents", retriever_sink)

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
    if "reranker" in nodes:
        inputs["reranker"] = {"query": query}

    result = pipeline.run(inputs)
    return result.get("excerpt_truncator", {}).get("documents", [])
