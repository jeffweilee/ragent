"""T3.2 — Ingest pipeline factory: Convert→Clean→[Idempotency]→Chunker→Embed→Write (B1)."""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Literal

import langdetect
import nltk
from haystack.components.converters import TextFileToDocument
from haystack.components.preprocessors import DocumentCleaner
from haystack.components.writers import DocumentWriter
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy

from ragent.utility.env import int_env

AtomKind = Literal["line", "cjk_sent", "en_sent"]

# CJK-density profile: scripts that are scriptio-continua (no word spaces) and
# pack much more semantic content per character than Latin scripts. bge-m3
# tokenization scales roughly with character count for these scripts, so a
# smaller char budget keeps the per-chunk token count comparable to EN.
_CJK_LANGS = frozenset({"zh-cn", "zh-tw", "zh", "ja", "ko", "th", "lo", "km", "my"})

_CHUNK_TARGET_CHARS_EN = int_env("CHUNK_TARGET_CHARS_EN", 2000)
_CHUNK_OVERLAP_CHARS_EN = int_env("CHUNK_OVERLAP_CHARS_EN", 300)
_CHUNK_TARGET_CHARS_CJK = int_env("CHUNK_TARGET_CHARS_CJK", 500)
_CHUNK_OVERLAP_CHARS_CJK = int_env("CHUNK_OVERLAP_CHARS_CJK", 100)
_CHUNK_TARGET_CHARS_CSV = int_env("CHUNK_TARGET_CHARS_CSV", 2000)
_CHUNK_OVERLAP_CHARS_CSV = int_env("CHUNK_OVERLAP_CHARS_CSV", 0)
_CHUNK_HARD_SPLIT_OVERLAP_CHARS = int_env("CHUNK_HARD_SPLIT_OVERLAP_CHARS", 200)

# Cap on how much text langdetect inspects; prefix is sufficient for accurate
# detection and avoids quadratic cost on large CSV/JSON dumps.
_LANG_DETECT_SAMPLE_CHARS = 1024
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
        stamped = [
            dataclasses.replace(d, meta={**d.meta, "document_id": document_id}) for d in documents
        ]
        return {"documents": stamped}


def _select_profile(doc: Document) -> tuple[int, int, AtomKind]:
    """Pick (target_chars, overlap_chars, atom_kind) for a document.

    Detection failures fall back to EN — safer (larger budget) than
    over-shrinking real prose.
    """
    if doc.meta.get("content_type") == "text/csv":
        return _CHUNK_TARGET_CHARS_CSV, _CHUNK_OVERLAP_CHARS_CSV, "line"
    sample = (doc.content or "")[:_LANG_DETECT_SAMPLE_CHARS]
    try:
        lang = langdetect.detect(sample)
    except langdetect.lang_detect_exception.LangDetectException:
        lang = "en"
    if lang in _CJK_LANGS:
        return _CHUNK_TARGET_CHARS_CJK, _CHUNK_OVERLAP_CHARS_CJK, "cjk_sent"
    return _CHUNK_TARGET_CHARS_EN, _CHUNK_OVERLAP_CHARS_EN, "en_sent"


def _segment(content: str, kind: AtomKind) -> list[str]:
    if kind == "line":
        return [line for line in content.splitlines() if line]
    if kind == "cjk_sent":
        return [s for s in _CJK_SENT_SPLIT_RE.split(content) if s.strip()]
    try:
        return [s for s in nltk.sent_tokenize(content) if s.strip()]
    except LookupError:  # pragma: no cover - punkt not provisioned
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
        # Walk backwards collecting tail atoms until cumulative length covers
        # overlap, then keep that suffix as the seed for the next chunk.
        carry_len = 0
        cut = len(buf)
        for i in range(len(buf) - 1, -1, -1):
            carry_len += len(buf[i]) + (len(joiner) if i < len(buf) - 1 else 0)
            cut = i
            if carry_len >= overlap:
                break
        buf = buf[cut:]
        buf_len = carry_len

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

            offset = 0
            for i, chunk_text in enumerate(_build_chunks(atoms, target, overlap, joiner)):
                # Search forward from offset to find the chunk's actual start
                # in the original content; avoids drift from variable carry length.
                key = chunk_text[: min(40, len(chunk_text))]
                found = content.find(key, offset)
                if found >= 0:
                    offset = found
                result.append(
                    Document(
                        content=chunk_text,
                        meta={**doc.meta, "split_id": i, "split_idx_start": offset},
                    )
                )
                offset += len(chunk_text)
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
