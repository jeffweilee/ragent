"""T3.2e — Pipeline retry: idempotency-clean purges prior chunks before re-indexing (R4, S25)."""

import dataclasses
from unittest.mock import AsyncMock

import pytest

from tests.conftest import FakeDocumentStore as _FakeStore
from tests.conftest import run_in_threadpool

pytestmark = pytest.mark.docker


def _loader_input(text: str, document_id: str) -> dict:
    return {
        "loader": {
            "content": text,
            "mime_type": "text/plain",
            "document_id": document_id,
        },
        "idempotency_clean": {"document_id": document_id},
    }


def test_pipeline_clears_chunks_before_rerun():
    """On retry, delete_by_document_id runs before new chunks are written."""
    from haystack.core.component import component
    from haystack.dataclasses import Document

    from ragent.pipelines.factory import build_ingest_pipeline

    delete_calls: list[str] = []
    chunk_repo = AsyncMock()
    chunk_repo.delete_by_document_id.side_effect = lambda doc_id: delete_calls.append(doc_id)

    @component
    class _MockEmbedder:
        @component.output_types(documents=list[Document])
        def run(self, documents: list[Document]) -> dict:
            return {
                "documents": [dataclasses.replace(doc, embedding=[0.0] * 4) for doc in documents]
            }

    pipeline = build_ingest_pipeline(
        embedder=_MockEmbedder(), document_store=_FakeStore(), chunk_repo=chunk_repo
    )

    text = "Hello world. Second sentence. Third sentence."
    result = run_in_threadpool(
        lambda: pipeline.run(_loader_input(text, "DOC001"), include_outputs_from={"embedder"})
    )
    docs = result["embedder"]["documents"]

    assert "DOC001" in delete_calls
    assert len(docs) > 0


def test_pipeline_retry_produces_no_duplicate_chunks():
    """Running the pipeline twice on same document_id produces exactly one set of chunks."""
    from haystack.core.component import component
    from haystack.dataclasses import Document

    from ragent.pipelines.factory import build_ingest_pipeline

    store = _FakeStore()
    chunk_repo = AsyncMock()
    # Simulate idempotency: delete prior writes before each rerun.
    chunk_repo.delete_by_document_id.side_effect = lambda doc_id: store.written.clear()

    @component
    class _MockEmbedder:
        @component.output_types(documents=list[Document])
        def run(self, documents: list[Document]) -> dict:
            return {
                "documents": [dataclasses.replace(doc, embedding=[0.0] * 4) for doc in documents]
            }

    text = "One sentence. Two sentences."

    def _run() -> None:
        pipeline = build_ingest_pipeline(
            embedder=_MockEmbedder(), document_store=store, chunk_repo=chunk_repo
        )
        run_in_threadpool(lambda: pipeline.run(_loader_input(text, "DOC001")))

    _run()
    first_count = len(store.written)
    _run()
    assert len(store.written) == first_count  # no duplicates after retry
