"""Phase 1 W3 cycle 3.1 — VectorExtractor unit contract (spec §2 Indexing Pipeline)."""

from dataclasses import dataclass, field
from typing import Any

from ragent.plugins import ExtractorPlugin
from ragent.plugins.vector import Chunk, VectorExtractor


@dataclass
class _Doc:
    source_title: str = "Title"
    source_app: str = "app"
    source_meta: str | None = None


@dataclass
class _Repo:
    doc: _Doc = field(default_factory=_Doc)

    def get(self, document_id: str) -> _Doc:
        return self.doc


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


def _chunks(document_id: str) -> list[Chunk]:
    return [
        Chunk(chunk_id=f"{document_id}_0", document_id=document_id, ord=0, text="hello", lang="en"),
        Chunk(chunk_id=f"{document_id}_1", document_id=document_id, ord=1, text="world", lang="en"),
    ]


def _make(document_id: str = "d1", doc: _Doc | None = None) -> VectorExtractor:
    d = doc or _Doc()
    return VectorExtractor(
        repo=_Repo(doc=d),
        chunks={document_id: _chunks(document_id)},
        embedder=_FakeEmbedder(),
        es=_FakeES(),
    )


def test_vector_extractor_conforms_to_protocol() -> None:
    plugin = _make()
    assert isinstance(plugin, ExtractorPlugin)
    assert plugin.name == "vector"
    assert plugin.required is True
    assert plugin.queue == "extract.vector"


def test_extract_calls_embedder_once_and_es_bulk_once() -> None:
    embedder, es = _FakeEmbedder(), _FakeES()
    plugin = VectorExtractor(repo=_Repo(), chunks={"d1": _chunks("d1")}, embedder=embedder, es=es)
    plugin.extract("d1")
    assert len(embedder.calls) == 1
    assert len(es.bulk_calls) == 1
    assert {a["_id"] for a in es.bulk_calls[0]} == {"d1_0", "d1_1"}
    assert all("embedding" in a["_source"] for a in es.bulk_calls[0])


def test_extract_is_idempotent_on_rerun() -> None:
    embedder, es = _FakeEmbedder(), _FakeES()
    plugin = VectorExtractor(repo=_Repo(), chunks={"d1": _chunks("d1")}, embedder=embedder, es=es)
    plugin.extract("d1")
    plugin.extract("d1")
    assert es.indexed_ids == {"d1_0", "d1_1"}


def test_delete_removes_all_chunks_for_doc() -> None:
    embedder, es = _FakeEmbedder(), _FakeES()
    plugin = VectorExtractor(repo=_Repo(), chunks={"d1": _chunks("d1")}, embedder=embedder, es=es)
    plugin.extract("d1")
    plugin.delete("d1")
    assert es.indexed_ids == set()


def test_health_true_when_dependencies_present() -> None:
    plugin = VectorExtractor(repo=_Repo(), chunks={}, embedder=_FakeEmbedder(), es=_FakeES())
    assert plugin.health() is True


def test_extract_is_noop_when_doc_not_found() -> None:
    """No error when the document was deleted between submission and processing."""

    class _MissingRepo:
        def get(self, document_id: str) -> None:
            return None

    es = _FakeES()
    plugin = VectorExtractor(
        repo=_MissingRepo(),
        chunks={"d1": _chunks("d1")},
        embedder=_FakeEmbedder(),
        es=es,
    )
    plugin.extract("d1")  # must not raise
    assert es.bulk_calls == []
