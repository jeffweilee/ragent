"""C4 / T2v.30-T2v.39 — v2 ingest pipeline factory.

Graph: ``_TextLoader → _MimeAwareSplitter → [_IdempotencyClean] →
_BudgetChunker → DocumentEmbedder → DocumentWriter`` (ES only).

Splitter dispatches per ``meta["mime_type"]``:
- ``text/plain``    → Haystack ``DocumentSplitter`` (passage)
- ``text/markdown`` → ``_MarkdownASTSplitter`` (mistletoe)
- ``text/html``     → ``_HtmlASTSplitter`` (selectolax)

Each splitter emits atoms whose ``meta["raw_content"]`` is the original
byte slice (markdown fences / HTML fragments preserved). ``_BudgetChunker``
is mime-agnostic: greedy-pack to ``CHUNK_TARGET_CHARS``, hard-split atoms
exceeding ``CHUNK_MAX_CHARS`` with ``CHUNK_OVERLAP_CHARS`` overlap.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import anyio.from_thread
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.core.component import component
from haystack.core.pipeline import Pipeline
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy

from ragent.pipelines.observability import IngestStepError, wrap_component_run
from ragent.utility.env import int_env

# Single mime-agnostic budget profile (replaces v1 EN/CJK/CSV constants).
CHUNK_TARGET_CHARS = int_env("CHUNK_TARGET_CHARS", 1000)
CHUNK_MAX_CHARS = int_env("CHUNK_MAX_CHARS", 1500)
CHUNK_OVERLAP_CHARS = int_env("CHUNK_OVERLAP_CHARS", 100)

ALLOWED_MIMES = ("text/plain", "text/markdown", "text/html")


# ---------------------------------------------------------------------------
# _TextLoader (T2v.30/31)
# ---------------------------------------------------------------------------


@component
class _TextLoader:
    """Build a single ``Document`` from inline content + per-document meta.

    The worker calls ``run(content=..., mime_type=..., document_id=...)`` so
    the loader replaces v1's ``TextFileToDocument`` + tempfile dance.
    """

    @component.output_types(documents=list[Document])
    def run(
        self,
        content: str,
        mime_type: str,
        document_id: str | None = None,
        source_url: str | None = None,
        source_title: str | None = None,
        source_app: str | None = None,
        source_workspace: str | None = None,
    ) -> dict:
        meta: dict[str, Any] = {"mime_type": mime_type, "content_type": mime_type}
        for k, v in (
            ("document_id", document_id),
            ("source_url", source_url),
            ("source_title", source_title),
            ("source_app", source_app),
            ("source_workspace", source_workspace),
        ):
            if v is not None:
                meta[k] = v
        return {"documents": [Document(content=content, meta=meta)]}


# ---------------------------------------------------------------------------
# _MarkdownASTSplitter (T2v.32/33)
# ---------------------------------------------------------------------------


_MD_BLOCK_TYPES = (
    "Heading",
    "Paragraph",
    "CodeFence",
    "List",
    "Table",
    "Quote",
    "ThematicBreak",
    "HtmlBlock",
)


@component
class _MarkdownASTSplitter:
    """Top-level markdown blocks → one atom each. Fenced code blocks are
    never split. ``meta["raw_content"]`` is the rendered markdown source of
    the block (markers preserved).
    """

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        import mistletoe
        from mistletoe.markdown_renderer import MarkdownRenderer

        atoms: list[Document] = []
        for doc in documents:
            content = doc.content or ""
            with MarkdownRenderer() as renderer:
                root = mistletoe.Document(content)
                for tok in root.children:
                    type_name = type(tok).__name__
                    if type_name not in _MD_BLOCK_TYPES:
                        continue  # BlankLine / etc.
                    raw = renderer.render(tok)
                    if not raw.strip():
                        continue
                    atoms.append(Document(content=raw, meta={**doc.meta, "raw_content": raw}))
        return {"documents": atoms}


# ---------------------------------------------------------------------------
# _HtmlASTSplitter (T2v.34/35)
# ---------------------------------------------------------------------------


_HTML_DROP_TAGS = ("script", "style", "nav", "aside", "footer", "header")
_HTML_ATOM_SELECTORS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "pre",
    "table",
    "blockquote",
)
_HTML_ATOM_TAGSET = frozenset(_HTML_ATOM_SELECTORS)


@component
class _HtmlASTSplitter:
    """Walks HTML DOM. Drops ``<script>/<style>/<nav>/<aside>/<footer>/<header>``
    (when not nested in ``<article>``/``<main>``); emits one atom per
    block-level element (headings, paragraphs, ``<pre>``, ``<table>``,
    ``<blockquote>``). ``meta["raw_content"]`` is the serialized outer HTML.
    """

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        from selectolax.parser import HTMLParser

        atoms: list[Document] = []
        for doc in documents:
            content = doc.content or ""
            tree = HTMLParser(content)
            self._strip_boilerplate(tree)
            for sel in _HTML_ATOM_SELECTORS:
                for node in tree.css(sel):
                    if self._has_atom_ancestor(node):
                        continue
                    text = node.text(deep=True, separator=" ", strip=True)
                    raw = node.html or text
                    if not text.strip():
                        continue
                    atoms.append(
                        Document(
                            content=text,
                            meta={**doc.meta, "raw_content": raw},
                        )
                    )
        return {"documents": atoms}

    @staticmethod
    def _strip_boilerplate(tree: Any) -> None:
        for tag in _HTML_DROP_TAGS:
            for node in tree.css(tag):
                # Keep when nested in an explicit content region.
                anc = node.parent
                inside_content = False
                while anc is not None:
                    if anc.tag in ("article", "main"):
                        inside_content = True
                        break
                    anc = anc.parent
                if not inside_content:
                    node.decompose()

    @staticmethod
    def _has_atom_ancestor(node: Any) -> bool:
        anc = node.parent
        while anc is not None:
            if anc.tag in _HTML_ATOM_TAGSET:
                return True
            anc = anc.parent
        return False


# ---------------------------------------------------------------------------
# _MimeAwareSplitter (T2v.38/39 — replaces FileTypeRouter+joiner+3-splitters)
# ---------------------------------------------------------------------------


@component
class _MimeAwareSplitter:
    """Routes Documents to the right splitter based on ``meta["mime_type"]``.

    Single component (not a Haystack ``FileTypeRouter`` + ``DocumentJoiner``
    pair) because Haystack's stock router routes ``ByteStream`` / ``Path``,
    not ``Document``. The plan graph and this implementation are equivalent:
    one fan-in, one fan-out, mime-driven dispatch, unknown → fail.
    """

    def __init__(self) -> None:
        # split_length is in passages; we treat the whole text as one passage
        # and let _BudgetChunker handle sizing.
        self._plain = DocumentSplitter(split_by="passage", split_length=1, split_overlap=0)
        self._plain.warm_up()
        self._md = _MarkdownASTSplitter()
        self._html = _HtmlASTSplitter()

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        atoms: list[Document] = []
        for doc in documents:
            mime = doc.meta.get("mime_type") or doc.meta.get("content_type") or "text/plain"
            if mime == "text/plain":
                out = self._plain.run([doc])["documents"]
                # DocumentSplitter doesn't set raw_content; default to its content.
                for a in out:
                    a.meta.setdefault("raw_content", a.content or "")
                    a.meta.setdefault("mime_type", mime)
            elif mime == "text/markdown":
                out = self._md.run([doc])["documents"]
            elif mime == "text/html":
                out = self._html.run([doc])["documents"]
            else:
                raise IngestStepError(
                    f"unroutable mime: {mime!r}", error_code="PIPELINE_UNROUTABLE"
                )
            atoms.extend(out)
        return {"documents": atoms}


# ---------------------------------------------------------------------------
# _IdempotencyClean — kept (legacy DB cleanup; v2 cleanup commit will drop)
# ---------------------------------------------------------------------------


@component
class _IdempotencyClean:
    """Deletes prior chunks before re-indexing to prevent duplicates on retry.

    Stamps ``document_id`` on every atom regardless of whether a chunk_repo
    is supplied (some tests construct without a repo).
    """

    def __init__(self, chunk_repo: Any | None = None) -> None:
        self._repo = chunk_repo

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document], document_id: str) -> dict:
        if self._repo is not None:
            anyio.from_thread.run(self._repo.delete_by_document_id, document_id)
        stamped = [
            dataclasses.replace(d, meta={**d.meta, "document_id": document_id}) for d in documents
        ]
        return {"documents": stamped}


# ---------------------------------------------------------------------------
# _BudgetChunker (T2v.36/37 — replaces _CharBudgetChunker)
# ---------------------------------------------------------------------------


@component
class _BudgetChunker:
    """Mime-agnostic budget chunker.

    Greedy-packs atoms into chunks ≤ ``CHUNK_TARGET_CHARS`` joined by
    newlines. Atoms longer than ``CHUNK_MAX_CHARS`` are hard-split with
    ``CHUNK_OVERLAP_CHARS`` overlap. Each output ``Document`` carries:
    - ``content``: packed normalized text
    - ``meta["raw_content"]``: concatenation of source atoms' raw slices
    - ``meta["split_id"]``: zero-based per-document index
    """

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        result: list[Document] = []
        # Group by document_id so split_id resets per source document.
        groups: dict[Any, list[Document]] = {}
        order: list[Any] = []
        for d in documents:
            key = d.meta.get("document_id")
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(d)

        for doc_id in order:
            atoms = groups[doc_id]
            if not atoms:
                continue
            base_meta = {**atoms[0].meta}
            base_meta.pop("raw_content", None)
            chunks = _pack_atoms(atoms)
            for i, (text, raw) in enumerate(chunks):
                meta = {**base_meta, "split_id": i, "raw_content": raw}
                result.append(Document(content=text, meta=meta))
        return {"documents": result}


def _pack_atoms(atoms: list[Document]) -> list[tuple[str, str]]:
    target = CHUNK_TARGET_CHARS
    max_chars = CHUNK_MAX_CHARS
    overlap = CHUNK_OVERLAP_CHARS
    chunks: list[tuple[str, str]] = []
    buf_text = ""
    buf_raw = ""

    def flush(carry: bool = True) -> None:
        nonlocal buf_text, buf_raw
        if not buf_text:
            return
        chunks.append((buf_text, buf_raw))
        if carry and overlap > 0 and len(buf_text) > overlap:
            buf_text = buf_text[-overlap:]
            buf_raw = buf_raw[-overlap:] if len(buf_raw) >= overlap else buf_raw
        else:
            buf_text = ""
            buf_raw = ""

    for atom in atoms:
        text = atom.content or ""
        raw = atom.meta.get("raw_content") or text
        if not text:
            continue
        if len(text) > max_chars:
            flush(carry=False)
            step = max(1, target - overlap)
            start = 0
            while start < len(text):
                end = min(start + target, len(text))
                piece = text[start:end]
                # Approximate raw slice by same window when raw and text align.
                raw_piece = raw[start:end] if len(raw) == len(text) else raw
                chunks.append((piece, raw_piece))
                if end == len(text):
                    break
                start += step
            continue
        sep = "\n" if buf_text else ""
        if buf_text and len(buf_text) + len(sep) + len(text) > target:
            flush(carry=True)
            sep = "\n" if buf_text else ""
        buf_text = buf_text + sep + text
        buf_raw = buf_raw + sep + raw
    if buf_text:
        chunks.append((buf_text, buf_raw))
    return chunks


# ---------------------------------------------------------------------------
# DocumentEmbedder — unchanged
# ---------------------------------------------------------------------------


@component
class DocumentEmbedder:
    """Wraps the project's external EmbeddingClient as a Haystack component."""

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


# ---------------------------------------------------------------------------
# build_ingest_pipeline — v2 graph
# ---------------------------------------------------------------------------


def build_ingest_pipeline(
    embedder: Any,
    document_store: Any,
    *,
    chunk_repo: Any | None = None,
) -> Pipeline:
    """V2 ingest pipeline.

    Run input shape::

        {
            "loader": {
                "content": str,
                "mime_type": "text/plain"|"text/markdown"|"text/html",
                "document_id": str,
                "source_url": str | None,
                "source_title": str | None,
                ...
            },
            "idempotency_clean": {"document_id": str},  # only when chunk_repo supplied
        }
    """
    pipeline = Pipeline()
    pipeline.add_component("loader", wrap_component_run(_TextLoader(), step="load"))
    pipeline.add_component(
        "splitter",
        wrap_component_run(_MimeAwareSplitter(), step="split", error_code="PIPELINE_UNROUTABLE"),
    )
    if chunk_repo is not None:
        pipeline.add_component(
            "idempotency_clean",
            wrap_component_run(_IdempotencyClean(chunk_repo), step="idempotency_clean"),
        )
    pipeline.add_component("chunker", wrap_component_run(_BudgetChunker(), step="chunker"))
    pipeline.add_component(
        "embedder", wrap_component_run(embedder, step="embedder", error_code="EMBEDDER_ERROR")
    )
    pipeline.add_component(
        "writer",
        wrap_component_run(
            DocumentWriter(document_store=document_store, policy=DuplicatePolicy.OVERWRITE),
            step="writer",
            error_code="ES_WRITE_ERROR",
        ),
    )

    pipeline.connect("loader.documents", "splitter.documents")
    if chunk_repo is not None:
        pipeline.connect("splitter.documents", "idempotency_clean.documents")
        pipeline.connect("idempotency_clean.documents", "chunker.documents")
    else:
        pipeline.connect("splitter.documents", "chunker.documents")
    pipeline.connect("chunker.documents", "embedder.documents")
    pipeline.connect("embedder.documents", "writer.documents")

    return pipeline
