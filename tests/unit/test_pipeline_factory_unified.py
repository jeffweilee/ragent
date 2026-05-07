"""V2 ingest pipeline graph + end-to-end runs (replaces v1 unified-builder tests)."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from haystack.core.component import component
from haystack.dataclasses import Document

from ragent.pipelines.factory import build_ingest_pipeline
from tests.conftest import FakeDocumentStore as _FakeStore


@component
class _MockEmbedder:
    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        return {"documents": [dataclasses.replace(d, embedding=[0.0, 1.0]) for d in documents]}


def test_v2_builder_basic_graph_has_v2_nodes() -> None:
    store = _FakeStore()
    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=store)

    nodes = set(pipeline.graph.nodes)
    assert {"loader", "splitter", "chunker", "embedder", "writer"} <= nodes
    # v1 graph names are gone
    assert "converter" not in nodes
    assert "cleaner" not in nodes
    assert "language_router" not in nodes
    assert "row_merger" not in nodes


def test_v2_builder_with_chunk_repo_includes_idempotency() -> None:
    store = _FakeStore()
    chunk_repo = MagicMock()
    pipeline = build_ingest_pipeline(
        embedder=_MockEmbedder(), document_store=store, chunk_repo=chunk_repo
    )
    assert "idempotency_clean" in pipeline.graph.nodes


def test_v2_builder_runs_end_to_end_and_writes() -> None:
    store = _FakeStore()
    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=store)

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

    assert len(store.written) >= 1
    for doc in store.written:
        assert doc.embedding == [0.0, 1.0]
        assert doc.meta.get("split_id") is not None
        assert doc.meta.get("raw_content")
