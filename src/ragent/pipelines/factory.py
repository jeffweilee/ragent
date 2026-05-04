"""T3.2 — Ingest pipeline factory: Convert→Clean→LanguageRouter→{cjk|en}Splitter→Embed (B1)."""

from __future__ import annotations

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
