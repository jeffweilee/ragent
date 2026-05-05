"""Tests for the unified ingest pipeline builder (collapsed from 3 → 1)."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from haystack.core.component import component
from haystack.dataclasses import ByteStream, Document

from ragent.pipelines.factory import build_ingest_pipeline
from tests.conftest import FakeDocumentStore as _FakeStore


@component
class _MockEmbedder:
    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        return {"documents": [dataclasses.replace(d, embedding=[0.0, 1.0]) for d in documents]}


def test_unified_builder_basic_graph_has_writer() -> None:
    store = _FakeStore()
    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=store)

    nodes = set(pipeline.graph.nodes)
    assert {"converter", "cleaner", "chunker", "embedder", "writer"} <= nodes
    assert "language_router" not in nodes
    assert "en_splitter" not in nodes
    assert "cjk_splitter" not in nodes
    assert "mime_router" not in nodes
    assert "row_merger" not in nodes


def test_unified_builder_with_chunk_repo_includes_idempotency() -> None:
    store = _FakeStore()
    chunk_repo = MagicMock()
    pipeline = build_ingest_pipeline(
        embedder=_MockEmbedder(), document_store=store, chunk_repo=chunk_repo
    )
    assert "idempotency_clean" in pipeline.graph.nodes


def test_unified_builder_runs_end_to_end_and_writes() -> None:
    store = _FakeStore()
    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=store)

    text = "Hello world. " * 200
    pipeline.run({"converter": {"sources": [ByteStream(data=text.encode())]}})

    assert len(store.written) >= 1
    for doc in store.written:
        assert doc.embedding == [0.0, 1.0]
        assert doc.meta.get("split_id") is not None
