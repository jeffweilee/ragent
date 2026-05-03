"""Phase 1 W3 cycle 3.1 — VectorExtractor unit contract (spec §2 Indexing Pipeline)."""

from dataclasses import dataclass, field
from typing import Any

from ragent.plugins import ExtractorPlugin
from ragent.plugins.vector import Chunk, VectorExtractor


@dataclass
class _FakeEmbedder:
    calls: list[list[str]] = field(default_factory=list)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(i)] * 4 for i, _ in enumerate(texts)]


@dataclass
class _FakeES:
    bulk_calls: list[list[dict[str, Any]]] = field(default_factory=list)
    indexed_ids: set[str] = field(default_factory=set)

    def bulk(self, actions: list[dict[str, Any]]) -> None:
        self.bulk_calls.append(list(actions))
        for a in actions:
            if a.get("_op_type") == "delete":
                self.indexed_ids.discard(a["_id"])
            else:
                self.indexed_ids.add(a["_id"])


def _chunks(doc_id: str) -> list[Chunk]:
    return [
        Chunk(chunk_id=f"{doc_id}_0", doc_id=doc_id, ord=0, text="hello", lang="en"),
        Chunk(chunk_id=f"{doc_id}_1", doc_id=doc_id, ord=1, text="world", lang="en"),
    ]


def _store(doc_id: str) -> dict[str, list[Chunk]]:
    return {doc_id: _chunks(doc_id)}


def test_vector_extractor_conforms_to_protocol() -> None:
    plugin = VectorExtractor(embedder=_FakeEmbedder(), es=_FakeES(), chunk_store=_store("d1"))
    assert isinstance(plugin, ExtractorPlugin)
    assert plugin.name == "vector"
    assert plugin.required is True
    assert plugin.queue == "extract.vector"


def test_extract_calls_embedder_once_and_es_bulk_once() -> None:
    embedder, es = _FakeEmbedder(), _FakeES()
    plugin = VectorExtractor(embedder=embedder, es=es, chunk_store=_store("d1"))

    plugin.extract("d1")

    assert len(embedder.calls) == 1
    assert embedder.calls[0] == ["hello", "world"]
    assert len(es.bulk_calls) == 1
    assert {a["_id"] for a in es.bulk_calls[0]} == {"d1_0", "d1_1"}
    assert all("embedding" in a["_source"] for a in es.bulk_calls[0])


def test_extract_is_idempotent_on_rerun() -> None:
    embedder, es = _FakeEmbedder(), _FakeES()
    plugin = VectorExtractor(embedder=embedder, es=es, chunk_store=_store("d1"))

    plugin.extract("d1")
    plugin.extract("d1")

    # Same chunk_ids must not produce duplicate ES docs (bulk uses _id upsert semantics).
    assert es.indexed_ids == {"d1_0", "d1_1"}


def test_delete_removes_all_chunks_for_doc() -> None:
    embedder, es = _FakeEmbedder(), _FakeES()
    plugin = VectorExtractor(embedder=embedder, es=es, chunk_store=_store("d1"))
    plugin.extract("d1")

    plugin.delete("d1")

    assert es.indexed_ids == set()


def test_health_true_when_dependencies_present() -> None:
    plugin = VectorExtractor(embedder=_FakeEmbedder(), es=_FakeES(), chunk_store={})
    assert plugin.health() is True
