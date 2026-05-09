"""T-RR.1 — `_SourceHydrator` drop-on-miss semantics (B36 / S6j).

A chunk whose `document_id` is not present in the READY rows returned by
`get_sources_by_document_ids` must be dropped — not passed through with
empty source fields. Decouples retrieval correctness from cleanup
completeness.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from haystack.dataclasses import Document

from ragent.pipelines.chat import _SourceHydrator
from tests.conftest import run_in_threadpool


def _hydrate(doc_repo, documents: list[Document]) -> list[Document]:
    # Hydrator uses anyio.from_thread.run; only legal on a worker thread.
    return run_in_threadpool(
        lambda: _SourceHydrator(doc_repo).run(documents=documents)["documents"]
    )


def test_hydrator_drops_chunk_when_document_id_missing_from_sources() -> None:
    """Orphan chunk (DB row deleted / non-READY) must not survive hydration."""
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {}  # no READY rows
    chunk = Document(content="orphan body", meta={"document_id": "ghost-doc"})

    result = _hydrate(doc_repo, [chunk])

    assert result == []


def test_hydrator_keeps_and_enriches_matched_chunk() -> None:
    """Match path: source fields populated; chunk preserved."""
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-1": ("app_a", "src-x", "Title A"),
    }
    chunk = Document(content="real body", meta={"document_id": "doc-1"})

    [hydrated] = _hydrate(doc_repo, [chunk])

    assert hydrated.meta["source_app"] == "app_a"
    assert hydrated.meta["source_id"] == "src-x"
    assert hydrated.meta["source_title"] == "Title A"


def test_hydrator_mixed_batch_keeps_only_matched_chunks() -> None:
    """Mixed retrieval result: only chunks whose doc is READY survive."""
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {
        "doc-live": ("app_a", "src-x", "Live"),
    }
    chunks = [
        Document(content="live", meta={"document_id": "doc-live"}),
        Document(content="orphan", meta={"document_id": "doc-deleted"}),
        Document(content="midflight", meta={"document_id": "doc-pending"}),
    ]

    result = _hydrate(doc_repo, chunks)

    assert [d.meta["document_id"] for d in result] == ["doc-live"]


def test_hydrator_drops_chunk_with_no_document_id() -> None:
    """Defence-in-depth: chunks lacking the document_id meta key are dropped."""
    doc_repo = AsyncMock()
    doc_repo.get_sources_by_document_ids.return_value = {}
    chunk = Document(content="no-id body", meta={})

    result = _hydrate(doc_repo, [chunk])

    assert result == []
