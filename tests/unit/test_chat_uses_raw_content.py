"""T2v.40 — Chat read-path prefers meta['raw_content'], falls back to content.

Reranker scores on `content` (stable); LLM context, excerpts, and source
envelopes use the `raw_content` fallback. `source_url` is exposed in
`sources[]`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from haystack.dataclasses import Document

from ragent.pipelines.chat import _ExcerptTruncator, _Reranker, doc_to_source_entry
from ragent.schemas.chat import ChatRequest, build_rag_messages


def _doc(content: str, *, raw_content: str | None = None, **meta) -> Document:
    m: dict = dict(meta)
    if raw_content is not None:
        m["raw_content"] = raw_content
    return Document(content=content, meta=m)


# --- LLM context builder ---


def test_llm_context_uses_raw_content_when_present() -> None:
    doc = _doc(
        "normalized text",
        raw_content="# Heading\n```py\nx=1\n```",
        document_id="d1",
        source_app="confluence",
        source_title="T",
    )
    req = ChatRequest(messages=[{"role": "user", "content": "q"}])
    result = build_rag_messages(req, [doc])
    ctx = result[-1]["content"]
    assert "# Heading" in ctx
    assert "```py" in ctx
    assert "normalized text" not in ctx


def test_llm_context_falls_back_to_content_when_raw_content_missing() -> None:
    doc = _doc("plain text only", document_id="d1", source_app="a", source_title="T")
    req = ChatRequest(messages=[{"role": "user", "content": "q"}])
    result = build_rag_messages(req, [doc])
    ctx = result[-1]["content"]
    assert "plain text only" in ctx


def test_llm_context_falls_back_to_content_when_raw_content_empty() -> None:
    doc = _doc("normalized", raw_content="", document_id="d1", source_app="a", source_title="T")
    req = ChatRequest(messages=[{"role": "user", "content": "q"}])
    result = build_rag_messages(req, [doc])
    assert "normalized" in result[-1]["content"]


# --- _ExcerptTruncator ---


def test_excerpt_truncator_uses_raw_content_when_present() -> None:
    doc = _doc("normalized", raw_content="RAW " * 200, document_id="d1")
    out = _ExcerptTruncator(max_chars=50).run(documents=[doc])["documents"]
    assert out[0].content.startswith("RAW")
    assert len(out[0].content) == 50


def test_excerpt_truncator_falls_back_to_content() -> None:
    doc = _doc("plain content here")
    out = _ExcerptTruncator(max_chars=512).run(documents=[doc])["documents"]
    assert out[0].content == "plain content here"


def test_excerpt_truncator_short_raw_unchanged() -> None:
    doc = _doc("normalized", raw_content="short raw")
    out = _ExcerptTruncator(max_chars=512).run(documents=[doc])["documents"]
    assert out[0].content == "short raw"


# --- Reranker keeps scoring on content ---


def test_reranker_scores_on_content_not_raw_content() -> None:
    rerank_client = MagicMock()
    rerank_client.rerank.return_value = [{"index": 0, "score": 0.9}]
    docs = [_doc("normalized text", raw_content="<RAW HTML>")]
    _Reranker(rerank_client, top_k=1).run(query="q", documents=docs)
    args, kwargs = rerank_client.rerank.call_args
    texts = kwargs.get("texts", args[1] if len(args) > 1 else None)
    assert texts == ["normalized text"]


# --- doc_to_source_entry ---


def test_doc_to_source_entry_includes_source_url() -> None:
    doc = SimpleNamespace(
        content="excerpt",
        meta={
            "document_id": "d1",
            "source_app": "a",
            "source_id": "s1",
            "source_title": "T",
            "source_url": "https://example.com/doc",
        },
    )
    entry = doc_to_source_entry(doc)
    assert entry["source_url"] == "https://example.com/doc"


def test_doc_to_source_entry_source_url_none_when_absent() -> None:
    doc = SimpleNamespace(content="excerpt", meta={"document_id": "d1"})
    entry = doc_to_source_entry(doc)
    assert entry["source_url"] is None


def test_doc_to_source_entry_includes_mime_type() -> None:
    doc = SimpleNamespace(
        content="excerpt",
        meta={"document_id": "d1", "mime_type": "text/markdown"},
    )
    entry = doc_to_source_entry(doc)
    assert entry["mime_type"] == "text/markdown"


def test_doc_to_source_entry_mime_type_falls_back_to_content_type() -> None:
    doc = SimpleNamespace(
        content="excerpt",
        meta={"document_id": "d1", "content_type": "text/html"},
    )
    entry = doc_to_source_entry(doc)
    assert entry["mime_type"] == "text/html"
