"""T3.2 — Ingest pipeline factory: Convert→Clean→LanguageRouter→{cjk|en}Splitter→Embed (B1)."""

from __future__ import annotations

import os
import re
from typing import Any

import langdetect
import nltk
from haystack.components.converters import TextFileToDocument
from haystack.components.preprocessors import DocumentCleaner, DocumentSplitter
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document

_CJK_LANGS = frozenset({"zh-cn", "zh-tw", "zh", "ja", "ko"})
_CSV_CHUNK_TARGET_CHARS = int(os.environ.get("CSV_CHUNK_TARGET_CHARS", "2000"))
_CJK_SENT_RE = re.compile(r"(?<=[。！？.!?])\s*")


@component
class _DocumentLanguageRouter:
    @component.output_types(en=list[Document], cjk=list[Document])
    def run(self, documents: list[Document]) -> dict:
        en, cjk = [], []
        for doc in documents:
            try:
                lang = langdetect.detect(doc.content or "")
            except langdetect.lang_detect_exception.LangDetectException:
                lang = "en"
            (cjk if lang in _CJK_LANGS else en).append(doc)
        return {"en": en, "cjk": cjk}


@component
class _CJKSentenceSplitter:
    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        result = []
        for doc in documents:
            sentences = [s for s in _CJK_SENT_RE.split(doc.content or "") if s.strip()]
            for i, sentence in enumerate(sentences):
                result.append(Document(content=sentence, meta={**doc.meta, "split_id": i}))
        return {"documents": result}


@component
class _RowMerger:
    """Merges CSV rows into chunks until buffer reaches CSV_CHUNK_TARGET_CHARS (B24)."""

    def __init__(self, target_chars: int = _CSV_CHUNK_TARGET_CHARS) -> None:
        self._target = target_chars

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        chunks: list[Document] = []
        buf: list[str] = []
        buf_len = 0
        for doc in documents:
            lines = (doc.content or "").splitlines()
            for line in lines:
                buf.append(line)
                buf_len += len(line)
                if buf_len >= self._target:
                    chunks.append(Document(content="\n".join(buf), meta=doc.meta))
                    buf, buf_len = [], 0
        if buf:
            last_meta = documents[-1].meta if documents else {}
            chunks.append(Document(content="\n".join(buf), meta=last_meta))
        return {"documents": chunks}


@component
class _MimeRouter:
    """Routes documents to 'csv' or 'other' based on content_type meta."""

    @component.output_types(csv=list[Document], other=list[Document])
    def run(self, documents: list[Document]) -> dict:
        csv_docs, other_docs = [], []
        for doc in documents:
            if doc.meta.get("content_type") == "text/csv":
                csv_docs.append(doc)
            else:
                other_docs.append(doc)
        return {"csv": csv_docs, "other": other_docs}


@component
class _IdempotencyClean:
    """Deletes prior chunks before re-indexing to prevent duplicates on retry (R4, S25)."""

    def __init__(self, chunk_repo: Any, document_id: str) -> None:
        self._repo = chunk_repo
        self._document_id = document_id

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        self._repo.delete_by_document_id(self._document_id)
        return {"documents": documents}


def build_ingest_pipeline(embedder: Any) -> Pipeline:
    nltk.download("punkt_tab", quiet=True)

    pipeline = Pipeline()
    pipeline.add_component("converter", TextFileToDocument())
    pipeline.add_component("cleaner", DocumentCleaner())
    pipeline.add_component("language_router", _DocumentLanguageRouter())
    pipeline.add_component(
        "en_splitter", DocumentSplitter(split_by="sentence", split_length=1, split_overlap=0)
    )
    pipeline.add_component("cjk_splitter", _CJKSentenceSplitter())
    pipeline.add_component("embedder", embedder)

    pipeline.connect("converter.documents", "cleaner.documents")
    pipeline.connect("cleaner.documents", "language_router.documents")
    pipeline.connect("language_router.en", "en_splitter.documents")
    pipeline.connect("language_router.cjk", "cjk_splitter.documents")
    pipeline.connect("en_splitter.documents", "embedder.documents")
    pipeline.connect("cjk_splitter.documents", "embedder.documents")

    return pipeline


def build_idempotent_ingest_pipeline(embedder: Any, chunk_repo: Any, document_id: str) -> Pipeline:
    """Build ingest pipeline with idempotency-clean step before embedding (R4, S25)."""
    nltk.download("punkt_tab", quiet=True)

    pipeline = Pipeline()
    pipeline.add_component("converter", TextFileToDocument())
    pipeline.add_component("cleaner", DocumentCleaner())
    pipeline.add_component("idempotency_clean", _IdempotencyClean(chunk_repo, document_id))
    pipeline.add_component("language_router", _DocumentLanguageRouter())
    pipeline.add_component(
        "en_splitter", DocumentSplitter(split_by="sentence", split_length=1, split_overlap=0)
    )
    pipeline.add_component("cjk_splitter", _CJKSentenceSplitter())
    pipeline.add_component("embedder", embedder)

    pipeline.connect("converter.documents", "cleaner.documents")
    pipeline.connect("cleaner.documents", "idempotency_clean.documents")
    pipeline.connect("idempotency_clean.documents", "language_router.documents")
    pipeline.connect("language_router.en", "en_splitter.documents")
    pipeline.connect("language_router.cjk", "cjk_splitter.documents")
    pipeline.connect("en_splitter.documents", "embedder.documents")
    pipeline.connect("cjk_splitter.documents", "embedder.documents")

    return pipeline


def build_csv_ingest_pipeline(embedder: Any) -> Pipeline:
    """Build ingest pipeline with MIME-conditional RowMerger for CSV (S35, B24)."""
    nltk.download("punkt_tab", quiet=True)

    pipeline = Pipeline()
    pipeline.add_component("converter", TextFileToDocument())
    pipeline.add_component("cleaner", DocumentCleaner())
    pipeline.add_component("mime_router", _MimeRouter())
    pipeline.add_component("row_merger", _RowMerger())
    pipeline.add_component("language_router", _DocumentLanguageRouter())
    pipeline.add_component(
        "en_splitter", DocumentSplitter(split_by="sentence", split_length=1, split_overlap=0)
    )
    pipeline.add_component("cjk_splitter", _CJKSentenceSplitter())
    pipeline.add_component("embedder", embedder)

    pipeline.connect("converter.documents", "cleaner.documents")
    pipeline.connect("cleaner.documents", "mime_router.documents")
    pipeline.connect("mime_router.csv", "row_merger.documents")
    pipeline.connect("mime_router.other", "language_router.documents")
    pipeline.connect("row_merger.documents", "embedder.documents")
    pipeline.connect("language_router.en", "en_splitter.documents")
    pipeline.connect("language_router.cjk", "cjk_splitter.documents")
    pipeline.connect("en_splitter.documents", "embedder.documents")
    pipeline.connect("cjk_splitter.documents", "embedder.documents")

    return pipeline
