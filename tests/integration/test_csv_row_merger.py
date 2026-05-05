"""T3.2k — Unified chunker: CSV and TXT both honor the 2000-char budget (S35, B24)."""

import dataclasses
import math

import pytest
from haystack.core.component import component
from haystack.dataclasses import ByteStream, Document

from tests.conftest import FakeDocumentStore as _FakeStore

pytestmark = pytest.mark.docker

_CHUNK_TARGET_CHARS_CSV = int(__import__("os").environ.get("CHUNK_TARGET_CHARS_CSV", "2000"))


@component
class _MockEmbedder:
    @component.output_types(documents=list[Document])
    def run(self, documents: list[Document]) -> dict:
        return {"documents": [dataclasses.replace(doc, embedding=[0.0] * 4) for doc in documents]}


def _make_csv(rows: int, chars_per_row: int = 50) -> ByteStream:
    row = "a" * chars_per_row
    content = "\n".join(row for _ in range(rows))
    return ByteStream(data=content.encode(), meta={"content_type": "text/csv"})


def _make_txt(total_chars: int) -> ByteStream:
    sentence = "This is a test sentence with enough words. "
    reps = max(1, total_chars // len(sentence))
    content = sentence * reps
    return ByteStream(data=content.encode(), meta={"content_type": "text/plain"})


def test_csv_10k_rows_chunk_count_bounded():
    from ragent.pipelines.factory import build_ingest_pipeline

    rows = 10_000
    chars_per_row = 50
    # Rows are joined by "\n" (1 char), so each chunk fits floor(target / (row+1)) rows.
    rows_per_chunk = _CHUNK_TARGET_CHARS_CSV // (chars_per_row + 1)
    expected_max = math.ceil(rows / rows_per_chunk) + 1

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=_FakeStore())
    result = pipeline.run(
        {"converter": {"sources": [_make_csv(rows, chars_per_row)]}},
        include_outputs_from={"embedder"},
    )
    docs = result["embedder"]["documents"]
    assert len(docs) <= expected_max, f"Expected ≤{expected_max} chunks, got {len(docs)}"


def test_txt_uses_sentence_packing_within_budget():
    """A .txt input is packed into ≤2000-char sentence chunks (EN profile)."""
    from ragent.pipelines.factory import build_ingest_pipeline

    rows = 100
    chars_per_row = 50
    total_chars = rows * chars_per_row

    pipeline = build_ingest_pipeline(embedder=_MockEmbedder(), document_store=_FakeStore())
    result = pipeline.run(
        {"converter": {"sources": [_make_txt(total_chars)]}},
        include_outputs_from={"embedder"},
    )
    docs = result["embedder"]["documents"]
    assert len(docs) >= 1
    for doc in docs:
        assert doc.content
        assert len(doc.content) <= 2200  # EN target 2000 + atom slack
