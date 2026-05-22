"""V2 ingest pipeline graph + end-to-end runs (replaces v1 unified-builder tests)."""

from __future__ import annotations

from haystack.core.component import component
from haystack.dataclasses import Document


@component
class _MockEmbedder:
    """Stand-in embedder that records received documents and returns [] (T-EM-R.6 contract)."""

    def __init__(self) -> None:
        self.received: list[Document] = []

    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        self.received.extend(documents)
        return {"documents": []}


def test_v2_builder_basic_graph_has_v2_nodes() -> None:
    from ragent.pipelines.factory import build_ingest_pipeline

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder())

    nodes = set(pipeline.graph.nodes)
    assert {"loader", "splitter", "chunker", "embedder"} <= nodes
    assert "writer" not in nodes
    # v1 graph names are gone
    assert "converter" not in nodes
    assert "cleaner" not in nodes
    assert "language_router" not in nodes
    assert "row_merger" not in nodes


def test_v2_builder_no_idempotency_clean_node() -> None:
    """C6 dropped _IdempotencyClean — retry idempotency is via OVERWRITE policy."""
    from ragent.pipelines.factory import build_ingest_pipeline

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder())
    assert "idempotency_clean" not in pipeline.graph.nodes


def test_v2_builder_runs_end_to_end_and_reaches_embedder() -> None:
    from ragent.pipelines.factory import build_ingest_pipeline

    embedder = _MockEmbedder()
    pipeline = build_ingest_pipeline(embedder=embedder)

    text = "Hello world. " * 200
    pipeline.run(
        {
            "loader": {
                "content": text,
                "mime_type": "text/plain",
                "document_id": "DOC-1",
            }
        }
    )

    assert len(embedder.received) >= 1
    for doc in embedder.received:
        assert doc.meta.get("split_id") is not None
        assert doc.meta.get("raw_content")
