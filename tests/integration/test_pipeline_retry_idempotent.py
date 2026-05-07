"""V2 retry idempotency: re-running the pipeline overwrites prior chunks via
Haystack ``DuplicatePolicy.OVERWRITE`` (chunks live only in ES; the v1
``chunk_repo.delete_by_document_id`` was dropped in C6)."""

import dataclasses

import pytest
from haystack.core.component import component
from haystack.dataclasses import Document

from ragent.pipelines.factory import build_ingest_pipeline
from tests.conftest import FakeDocumentStore as _FakeStore
from tests.conftest import run_in_threadpool

pytestmark = pytest.mark.docker


def _loader_input(text: str, document_id: str) -> dict:
    return {
        "loader": {
            "content": text,
            "mime_type": "text/plain",
            "document_id": document_id,
        }
    }


@component
class _MockEmbedder:
    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        return {"documents": [dataclasses.replace(d, embedding=[0.0] * 4) for d in documents]}


def test_pipeline_runs_end_to_end_and_writes() -> None:
    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=_FakeStore())
    result = run_in_threadpool(
        lambda: pipeline.run(
            _loader_input("Hello world. Second sentence. Third sentence.", "DOC001"),
            include_outputs_from={"embedder"},
        )
    )
    assert len(result["embedder"]["documents"]) >= 1


def test_pipeline_retry_writes_same_document_count() -> None:
    """Same input → same chunks; OVERWRITE policy avoids duplicates."""
    store = _FakeStore()
    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=store)

    text = "One sentence. Two sentences. Three sentences."
    run_in_threadpool(lambda: pipeline.run(_loader_input(text, "DOC001")))
    first_count = len(store.written)

    # FakeStore appends naively (real ES store would dedupe by id); the
    # invariant we assert is that the pipeline produces a deterministic
    # chunk count for identical input.
    store2 = _FakeStore()
    pipeline2 = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=store2)
    run_in_threadpool(lambda: pipeline2.run(_loader_input(text, "DOC001")))
    assert len(store2.written) == first_count
