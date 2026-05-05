"""T3.2 — Ingest pipeline factory: Convert→Clean→[Idempotency]→Chunker→Embed→Write (B1)."""

from __future__ import annotations

import dataclasses
import os
import re
from typing import Any

import langdetect
from haystack.components.converters import TextFileToDocument
from haystack.components.preprocessors import DocumentCleaner
from haystack.components.writers import DocumentWriter
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy

# CJK-density profile: scripts that are scriptio-continua (no word spaces) and
# pack much more semantic content per character than Latin scripts. bge-m3
# tokenization scales roughly with character count for these scripts, so a
# smaller char budget keeps the per-chunk token count comparable to EN.
_CJK_LANGS = frozenset({"zh-cn", "zh-tw", "zh", "ja", "ko", "th", "lo", "km", "my"})

_CHUNK_TARGET_CHARS_EN = int(os.environ.get("CHUNK_TARGET_CHARS_EN", "2000"))
_CHUNK_OVERLAP_CHARS_EN = int(os.environ.get("CHUNK_OVERLAP_CHARS_EN", "300"))
_CHUNK_TARGET_CHARS_CJK = int(os.environ.get("CHUNK_TARGET_CHARS_CJK", "500"))
_CHUNK_OVERLAP_CHARS_CJK = int(os.environ.get("CHUNK_OVERLAP_CHARS_CJK", "100"))
_CHUNK_TARGET_CHARS_CSV = int(os.environ.get("CHUNK_TARGET_CHARS_CSV", "2000"))
_CHUNK_OVERLAP_CHARS_CSV = int(os.environ.get("CHUNK_OVERLAP_CHARS_CSV", "0"))
_CHUNK_HARD_SPLIT_OVERLAP_CHARS = int(os.environ.get("CHUNK_HARD_SPLIT_OVERLAP_CHARS", "200"))
_CJK_SENT_SPLIT_RE = re.compile(r"(?<=[。！？.!?।])\s*")


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


@component
class DocumentEmbedder:
    """Wraps the project's external EmbeddingClient as a Haystack component.

    The custom EmbeddingClient (clients/embedding.py) speaks the third-party
    HTTP contract and is not a Haystack TextEmbedder. This thin wrapper lets
    it slot into ingest pipelines: takes a list of Documents, embeds their
    .content, and returns Documents with .embedding populated.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        if not documents:
            return {"documents": []}
        texts = [d.content or "" for d in documents]
        embeddings = self._client.embed(texts)
        out = [
            dataclasses.replace(d, embedding=e) for d, e in zip(documents, embeddings, strict=True)
        ]
        return {"documents": out}


def build_ingest_pipeline(
    embedder: Any,
    document_store: Any,
    *,
    chunk_repo: Any | None = None,
) -> Pipeline:
    """Unified ingest pipeline.

    Graph: Convert → Clean → [IdempotencyClean] → CharBudgetChunker →
    Embedder → DocumentWriter. When `chunk_repo` is supplied, the
    idempotency-clean step is inserted (document_id is passed at run time).
    """
    pipeline = Pipeline()
    pipeline.add_component("converter", TextFileToDocument())
    pipeline.add_component("cleaner", DocumentCleaner())
    if chunk_repo is not None:
        pipeline.add_component("idempotency_clean", _IdempotencyClean(chunk_repo))
    pipeline.add_component("chunker", _CharBudgetChunker())
    pipeline.add_component("embedder", embedder)
    pipeline.add_component(
        "writer",
        DocumentWriter(document_store=document_store, policy=DuplicatePolicy.OVERWRITE),
    )

    pipeline.connect("converter.documents", "cleaner.documents")
    if chunk_repo is not None:
        pipeline.connect("cleaner.documents", "idempotency_clean.documents")
        pipeline.connect("idempotency_clean.documents", "chunker.documents")
    else:
        pipeline.connect("cleaner.documents", "chunker.documents")
    pipeline.connect("chunker.documents", "embedder.documents")
    pipeline.connect("embedder.documents", "writer.documents")

    return pipeline
