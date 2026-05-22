"""V2 retry idempotency: re-running the pipeline produces the same chunk count.

The pipeline's chunking is deterministic; the embedder bulk-writes to ES
with ``_op_type: index`` (overwrite-by-id), so re-ingesting the same
document is idempotent at the ES level (chunks live only in ES; the v1
``chunk_repo.delete_by_document_id`` was dropped in C6)."""

import pytest
from haystack.core.component import component
from haystack.dataclasses import Document

from ragent.pipelines.factory import build_ingest_pipeline
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
class _RecordingEmbedder:
    """Stand-in embedder that records received chunks; returns [] (T-EM-R.6 contract)."""

    def __init__(self) -> None:
        self.received: list[Document] = []

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        self.received.extend(documents)
        return {"documents": []}


def test_pipeline_runs_end_to_end_and_reaches_embedder() -> None:
    embedder = _RecordingEmbedder()
    pipeline = build_ingest_pipeline(embedder=embedder)
    run_in_threadpool(
        lambda: pipeline.run(
            _loader_input("Hello world. Second sentence. Third sentence.", "DOC001"),
        )
    )
    assert len(embedder.received) >= 1


def test_pipeline_retry_produces_same_chunk_count() -> None:
    """Same input → same chunks; deterministic chunking ensures consistent count."""
    embedder1 = _RecordingEmbedder()
    pipeline1 = build_ingest_pipeline(embedder=embedder1)

    text = "One sentence. Two sentences. Three sentences."
    run_in_threadpool(lambda: pipeline1.run(_loader_input(text, "DOC001")))
    first_count = len(embedder1.received)

    embedder2 = _RecordingEmbedder()
    pipeline2 = build_ingest_pipeline(embedder=embedder2)
    run_in_threadpool(lambda: pipeline2.run(_loader_input(text, "DOC001")))
    assert len(embedder2.received) == first_count
