"""T3.1 — Ingest pipeline: Convert→Clean→LanguageRouter→{cjk|en}Splitter→Embed (B1)."""

import pytest
from haystack.core.component import component
from haystack.dataclasses import ByteStream, Document

pytestmark = pytest.mark.docker


@component
class _MockEmbedder:
    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        for doc in documents:
            doc.embedding = [0.0] * 4
        return {"documents": documents}


def _en_stream() -> ByteStream:
    text = "The quick brown fox jumps. A second sentence appears. And a third follows."
    return ByteStream(data=text.encode(), meta={"content_type": "text/plain"})


def _cjk_stream() -> ByteStream:
    text = "这是第一句话。这是第二句话。这是第三句话。"
    return ByteStream(data=text.encode("utf-8"), meta={"content_type": "text/plain"})


def test_en_doc_routed_to_en_splitter_sentence_count():
    from ragent.pipelines.factory import build_ingest_pipeline

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder())
    result = pipeline.run({"converter": {"sources": [_en_stream()]}})
    docs = result["embedder"]["documents"]
    assert len(docs) == 3, f"expected 3 EN sentences, got {len(docs)}: {[d.content for d in docs]}"


def test_cjk_doc_routed_to_cjk_splitter_sentence_count():
    from ragent.pipelines.factory import build_ingest_pipeline

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder())
    result = pipeline.run({"converter": {"sources": [_cjk_stream()]}})
    docs = result["embedder"]["documents"]
    assert len(docs) == 3, f"expected 3 CJK sentences, got {len(docs)}: {[d.content for d in docs]}"


def test_en_chunks_have_embeddings():
    from ragent.pipelines.factory import build_ingest_pipeline

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder())
    result = pipeline.run({"converter": {"sources": [_en_stream()]}})
    docs = result["embedder"]["documents"]
    for doc in docs:
        assert doc.embedding is not None and len(doc.embedding) == 4
