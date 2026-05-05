"""T3.2 — Ingest pipeline factory: Convert→Clean→LanguageRouter→{cjk|en}Splitter→Embed (B1)."""

from __future__ import annotations

import os
import re
from typing import Any

import langdetect
from haystack.components.converters import TextFileToDocument
from haystack.components.preprocessors import DocumentCleaner, DocumentSplitter
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document

# CJK-density profile: scripts that are scriptio-continua (no word spaces) and
# pack much more semantic content per character than Latin scripts. bge-m3
# tokenization scales roughly with character count for these scripts, so a
# smaller char budget keeps the per-chunk token count comparable to EN.
_CJK_LANGS = frozenset({"zh-cn", "zh-tw", "zh", "ja", "ko", "th", "lo", "km", "my"})
_CSV_CHUNK_TARGET_CHARS = int(os.environ.get("CSV_CHUNK_TARGET_CHARS", "2000"))
_CJK_SENT_RE = re.compile(r"(?<=[。！？.!?])\s*")

_CHUNK_TARGET_CHARS_EN = int(os.environ.get("CHUNK_TARGET_CHARS_EN", "2000"))
_CHUNK_OVERLAP_CHARS_EN = int(os.environ.get("CHUNK_OVERLAP_CHARS_EN", "300"))
_CHUNK_TARGET_CHARS_CJK = int(os.environ.get("CHUNK_TARGET_CHARS_CJK", "500"))
_CHUNK_OVERLAP_CHARS_CJK = int(os.environ.get("CHUNK_OVERLAP_CHARS_CJK", "100"))
_CHUNK_TARGET_CHARS_CSV = int(os.environ.get("CHUNK_TARGET_CHARS_CSV", "2000"))
_CHUNK_OVERLAP_CHARS_CSV = int(os.environ.get("CHUNK_OVERLAP_CHARS_CSV", "0"))
_CHUNK_HARD_SPLIT_OVERLAP_CHARS = int(os.environ.get("CHUNK_HARD_SPLIT_OVERLAP_CHARS", "200"))
_CJK_SENT_SPLIT_RE = re.compile(r"(?<=[。！？.!?।])\s*")


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
    """Deletes prior chunks before re-indexing to prevent duplicates on retry (R4, S25).

    document_id is a run input, not a constructor arg, so a single pipeline
    instance can be reused across documents.
    """

    def __init__(self, chunk_repo: Any) -> None:
        self._repo = chunk_repo

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document], document_id: str) -> dict:
        self._repo.delete_by_document_id(document_id)
        return {"documents": documents}


def _select_profile(doc: Document) -> tuple[int, int, str]:
    """Pick (target_chars, overlap_chars, atom_kind) for a document.

    atom_kind ∈ {"line", "cjk_sent", "en_sent"}. CSV is detected by
    content_type meta; otherwise full-text language detection picks
    between the EN and CJK-density profiles. Detection failures fall
    back to EN — safer (larger budget) than over-shrinking real prose.
    """
    if doc.meta.get("content_type") == "text/csv":
        return _CHUNK_TARGET_CHARS_CSV, _CHUNK_OVERLAP_CHARS_CSV, "line"
    try:
        lang = langdetect.detect(doc.content or "")
    except langdetect.lang_detect_exception.LangDetectException:
        lang = "en"
    if lang in _CJK_LANGS:
        return _CHUNK_TARGET_CHARS_CJK, _CHUNK_OVERLAP_CHARS_CJK, "cjk_sent"
    return _CHUNK_TARGET_CHARS_EN, _CHUNK_OVERLAP_CHARS_EN, "en_sent"


def _segment(content: str, kind: str) -> list[str]:
    if kind == "line":
        return [line for line in content.splitlines() if line]
    if kind == "cjk_sent":
        return [s for s in _CJK_SENT_SPLIT_RE.split(content) if s.strip()]
    # en_sent — try punkt; fall back to the same regex if unavailable
    try:
        import nltk

        return [s for s in nltk.sent_tokenize(content) if s.strip()]
    except (LookupError, Exception):  # pragma: no cover - punkt missing
        return [s for s in _CJK_SENT_SPLIT_RE.split(content) if s.strip()]


def _hard_split(atom: str, target: int, overlap: int) -> list[str]:
    """Char-window split with overlap for atoms that exceed the budget."""
    step = max(1, target - overlap)
    pieces: list[str] = []
    start = 0
    while start < len(atom):
        end = min(start + target, len(atom))
        pieces.append(atom[start:end])
        if end == len(atom):
            break
        start += step
    return pieces


def _pack_atoms(atoms: list[str], target: int, overlap: int, joiner: str) -> list[str]:
    """Greedy-pack atoms into chunks ≤ target; seed each new chunk with the
    trailing atoms of the previous one whose cumulative length ≥ overlap.
    """
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        chunks.append(joiner.join(buf))
        if overlap <= 0:
            buf, buf_len = [], 0
            return
        carry: list[str] = []
        carry_len = 0
        for atom in reversed(buf):
            carry.insert(0, atom)
            carry_len += len(atom) + (len(joiner) if len(carry) > 1 else 0)
            if carry_len >= overlap:
                break
        buf = carry
        buf_len = sum(len(a) for a in buf) + max(0, len(buf) - 1) * len(joiner)

    for atom in atoms:
        atom_len = len(atom)
        sep = len(joiner) if buf else 0
        if buf and buf_len + sep + atom_len > target:
            flush()
            sep = len(joiner) if buf else 0
        buf.append(atom)
        buf_len += sep + atom_len
    if buf:
        chunks.append(joiner.join(buf))
    return chunks


def _build_chunks(atoms: list[str], target: int, overlap: int, joiner: str) -> list[str]:
    """Pack atoms into chunks ≤ target. Atoms > target are emitted as
    standalone hard-split pieces, flushing any pending packed buffer first.
    """
    chunks: list[str] = []
    pending: list[str] = []

    def flush_pending() -> None:
        nonlocal pending
        if pending:
            chunks.extend(_pack_atoms(pending, target, overlap, joiner))
            pending = []

    for atom in atoms:
        if len(atom) > target:
            flush_pending()
            chunks.extend(_hard_split(atom, target, _CHUNK_HARD_SPLIT_OVERLAP_CHARS))
        else:
            pending.append(atom)
    flush_pending()
    return chunks


@component
class _CharBudgetChunker:
    """Unified per-language char-budget chunker (replaces language router +
    EN/CJK splitters + CSV row-merger). EN/other → 2000/300, CJK-density →
    500/100, CSV → 2000/0. Oversized atoms are hard-split with 200-char
    overlap. Mixed-language docs are bucketed per source document.
    """

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        result: list[Document] = []
        for doc in documents:
            content = doc.content or ""
            if not content:
                continue
            target, overlap, kind = _select_profile(doc)
            joiner = "\n" if kind == "line" else " "
            atoms = _segment(content, kind)
            if not atoms:
                continue

            cursor = 0
            for i, chunk_text in enumerate(_build_chunks(atoms, target, overlap, joiner)):
                idx = content.find(chunk_text[: min(50, len(chunk_text))], cursor)
                split_idx_start = idx if idx >= 0 else cursor
                cursor = max(cursor, split_idx_start + 1)
                result.append(
                    Document(
                        content=chunk_text,
                        meta={**doc.meta, "split_id": i, "split_idx_start": split_idx_start},
                    )
                )
        return {"documents": result}


def build_ingest_pipeline(embedder: Any) -> Pipeline:
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


def build_idempotent_ingest_pipeline(embedder: Any, chunk_repo: Any) -> Pipeline:
    """Build ingest pipeline with idempotency-clean step before embedding (R4, S25).

    The document_id is supplied per-run via pipeline.run inputs so a single
    pipeline instance can be reused across documents.
    """
    pipeline = Pipeline()
    pipeline.add_component("converter", TextFileToDocument())
    pipeline.add_component("cleaner", DocumentCleaner())
    pipeline.add_component("idempotency_clean", _IdempotencyClean(chunk_repo))
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
