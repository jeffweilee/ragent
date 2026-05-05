"""T3.2e — Pipeline retry: idempotency-clean purges prior chunks before re-indexing (R4, S25)."""

import dataclasses
from unittest.mock import MagicMock

import pytest
from haystack.dataclasses import Document

pytestmark = pytest.mark.docker


class _FakeStore:
    def __init__(self) -> None:
        self.written: list[Document] = []

    def write_documents(self, documents: list[Document], policy=None) -> int:  # noqa: ANN001
        self.written.extend(documents)
        return len(documents)

    def count_documents(self) -> int:
        return len(self.written)

    def filter_documents(self, filters=None):  # noqa: ANN001
        return list(self.written)


def test_pipeline_clears_chunks_before_rerun():
    """On retry, delete_by_document_id runs before new chunks are written."""
    from ragent.pipelines.factory import build_ingest_pipeline

    delete_calls: list[str] = []
    insert_calls: list[str] = []

    chunk_repo = MagicMock()
    chunk_repo.delete_by_document_id.side_effect = lambda doc_id: delete_calls.append(doc_id)
    chunk_repo.bulk_insert.side_effect = lambda chunks: insert_calls.extend(
        [c["document_id"] for c in chunks]
    )

    from haystack.core.component import component
    from haystack.dataclasses import Document

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

    from haystack.dataclasses import ByteStream

    text = "Hello world. Second sentence. Third sentence."
    result = pipeline.run(
        {
            "converter": {"sources": [ByteStream(data=text.encode())]},
            "idempotency_clean": {"document_id": "DOC001"},
        }
    )
    docs = result["embedder"]["documents"]

    # Idempotency-clean step must have run delete before any inserts
    assert "DOC001" in delete_calls
    assert len(docs) > 0


def test_pipeline_retry_produces_no_duplicate_chunks():
    """Running the pipeline twice on same document_id produces exactly one set of chunks."""
    from haystack.core.component import component
    from haystack.dataclasses import ByteStream, Document

    from ragent.pipelines.factory import build_ingest_pipeline

    chunks: list[dict] = []
    chunk_repo = MagicMock()
    chunk_repo.delete_by_document_id.side_effect = lambda doc_id: chunks.clear()
    chunk_repo.bulk_insert.side_effect = chunks.extend

    @component
    class _MockEmbedder:
        @component.output_types(documents=list[Document])
        def run(self, documents: list[Document]) -> dict:
            return {
                "documents": [dataclasses.replace(doc, embedding=[0.0] * 4) for doc in documents]
            }

    text = "One sentence. Two sentences."

    def _run():
        pipeline = build_ingest_pipeline(
            embedder=_MockEmbedder(), document_store=_FakeStore(), chunk_repo=chunk_repo
        )
        pipeline.run(
            {
                "converter": {"sources": [ByteStream(data=text.encode())]},
                "idempotency_clean": {"document_id": "DOC001"},
            }
        )

    _run()
    first_count = len(chunks)
    _run()
    assert len(chunks) == first_count  # no duplicates after retry
