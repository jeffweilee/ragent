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
    delete_by_query_calls: list[dict[str, Any]] = field(default_factory=list)
    _indexed: dict[str, str] = field(default_factory=dict)  # chunk_id -> document_id

    @property
    def indexed_ids(self) -> set[str]:
        return set(self._indexed.keys())

    def bulk(self, actions: list[dict[str, Any]]) -> None:
        self.bulk_calls.append(list(actions))
        for a in actions:
            if a.get("_op_type") == "delete":
                self._indexed.pop(a["_id"], None)
            else:
                self._indexed[a["_id"]] = a["_source"]["document_id"]

    def delete_by_query(
        self, *, index: str, query: dict[str, Any], conflicts: str = "proceed"
    ) -> None:
        self.delete_by_query_calls.append({"index": index, "query": query, "conflicts": conflicts})
        doc_id = query["term"]["document_id"]
        for k in [k for k, v in self._indexed.items() if v == doc_id]:
            del self._indexed[k]


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


def test_delete_uses_delete_by_query_with_chunks_empty() -> None:
    """v2 wiring passes chunks={} — delete() must clean up via delete_by_query, not bulk."""
    es = _FakeES()
    plugin = VectorExtractor(repo=_Repo(), chunks={}, embedder=_FakeEmbedder(), es=es)
    plugin.delete("d1")
    assert len(es.delete_by_query_calls) == 1
    call = es.delete_by_query_calls[0]
    assert call["index"] == "chunks_v1"
    assert call["query"] == {"term": {"document_id": "d1"}}
    assert call["conflicts"] == "proceed"


def test_delete_uses_delete_by_query_index_override() -> None:
    """Custom index name is forwarded to delete_by_query."""
    es = _FakeES()
    plugin = VectorExtractor(
        repo=_Repo(), chunks={}, embedder=_FakeEmbedder(), es=es, index="custom_idx"
    )
    plugin.delete("doc99")
    assert es.delete_by_query_calls[0]["index"] == "custom_idx"


# T-DEL1.1 — registry-aware delete


class _RegistryStub:
    def __init__(self, stable: str = "chunks_stable", candidate: str | None = None) -> None:
        self._stable = stable
        self._candidate = candidate

    @property
    def stable_index(self) -> str:
        return self._stable

    @property
    def candidate_index(self) -> str | None:
        return self._candidate


def test_delete_with_registry_stable_only() -> None:
    """When registry has no candidate, delete() targets stable_index only."""
    es = _FakeES()
    plugin = VectorExtractor(
        repo=_Repo(), chunks={}, embedder=_FakeEmbedder(), es=es, registry=_RegistryStub()
    )
    plugin.delete("doc1")
    assert len(es.delete_by_query_calls) == 1
    assert es.delete_by_query_calls[0]["index"] == "chunks_stable"
    assert es.delete_by_query_calls[0]["query"] == {"term": {"document_id": "doc1"}}


def test_delete_with_registry_dual_index() -> None:
    """During CANDIDATE/CUTOVER, delete() issues one call per live index."""
    es = _FakeES()
    plugin = VectorExtractor(
        repo=_Repo(),
        chunks={},
        embedder=_FakeEmbedder(),
        es=es,
        registry=_RegistryStub(candidate="chunks_candidate"),
    )
    plugin.delete("doc2")
    assert len(es.delete_by_query_calls) == 2
    indices = [c["index"] for c in es.delete_by_query_calls]
    assert indices == ["chunks_stable", "chunks_candidate"]
    for call in es.delete_by_query_calls:
        assert call["query"] == {"term": {"document_id": "doc2"}}
        assert call["conflicts"] == "proceed"


def test_delete_registry_takes_precedence_over_index_kwarg() -> None:
    """When registry is injected, it drives the index list — static index= is ignored for delete."""
    es = _FakeES()
    plugin = VectorExtractor(
        repo=_Repo(),
        chunks={},
        embedder=_FakeEmbedder(),
        es=es,
        index="old_index",
        registry=_RegistryStub(),
    )
    plugin.delete("doc3")
    assert len(es.delete_by_query_calls) == 1
    assert es.delete_by_query_calls[0]["index"] == "chunks_stable"


def test_delete_indices_deduplicates_when_candidate_equals_stable() -> None:
    """If candidate_index == stable_index, delete() must not issue duplicate calls."""
    es = _FakeES()
    plugin = VectorExtractor(
        repo=_Repo(),
        chunks={},
        embedder=_FakeEmbedder(),
        es=es,
        registry=_RegistryStub(stable="chunks_v1", candidate="chunks_v1"),
    )
    plugin.delete("doc4")
    assert len(es.delete_by_query_calls) == 1
    assert es.delete_by_query_calls[0]["index"] == "chunks_v1"
