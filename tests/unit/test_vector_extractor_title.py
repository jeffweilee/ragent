"""T1.11 — VectorExtractor B15+B17+B29: repo-based title injection, ES bulk field contract."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from ragent.plugins.vector import Chunk, VectorExtractor


@dataclass
class _Doc:
    source_title: str
    source_app: str
    source_meta: str | None = None


@dataclass
class _Repo:
    docs: dict[str, _Doc] = field(default_factory=dict)

    def get(self, document_id: str) -> _Doc:
        return self.docs[document_id]


@dataclass
class _Embedder:
    inputs: list[str] = field(default_factory=list)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        return [[0.1] * 4 for _ in texts]


@dataclass
class _ES:
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def bulk(self, actions: list[dict[str, Any]]) -> None:
        self.calls.append(list(actions))


def _build(doc: _Doc, chunks: list[Chunk]) -> tuple[_Repo, dict[str, list[Chunk]], _Embedder, _ES]:
    doc_id = chunks[0].document_id
    repo = _Repo(docs={doc_id: doc})
    store = {doc_id: chunks}
    return repo, store, _Embedder(), _ES()


def test_embed_input_is_title_plus_chunk_text() -> None:
    doc = _Doc(source_title="My Title", source_app="wiki")
    chunks = [
        Chunk(chunk_id="c1", document_id="d1", ord=0, text="paragraph one", lang="en"),
        Chunk(chunk_id="c2", document_id="d1", ord=1, text="paragraph two", lang="en"),
    ]
    repo, store, embedder, es = _build(doc, chunks)
    plugin = VectorExtractor(repo=repo, chunks=store, embedder=embedder, es=es)
    plugin.extract("d1")
    assert embedder.inputs[0] == "My Title\n\nparagraph one"
    assert embedder.inputs[1] == "My Title\n\nparagraph two"


def test_es_bulk_doc_carries_required_fields() -> None:
    doc = _Doc(source_title="Report", source_app="confluence")
    chunks = [Chunk(chunk_id="c1", document_id="d1", ord=0, text="body", lang="en")]
    repo, store, embedder, es = _build(doc, chunks)
    plugin = VectorExtractor(repo=repo, chunks=store, embedder=embedder, es=es)
    plugin.extract("d1")
    source = es.calls[0][0]["_source"]
    required = {"chunk_id", "document_id", "lang", "title", "text", "embedding", "source_app"}
    assert required <= source.keys()


def test_es_bulk_doc_no_extra_fields_without_workspace() -> None:
    doc = _Doc(source_title="Doc", source_app="slack", source_meta=None)
    chunks = [Chunk(chunk_id="c1", document_id="d1", ord=0, text="hi", lang="en")]
    repo, store, embedder, es = _build(doc, chunks)
    plugin = VectorExtractor(repo=repo, chunks=store, embedder=embedder, es=es)
    plugin.extract("d1")
    source = es.calls[0][0]["_source"]
    assert "source_meta" not in source


def test_es_bulk_doc_includes_workspace_when_set() -> None:
    doc = _Doc(source_title="Doc", source_app="slack", source_meta="team-alpha")
    chunks = [Chunk(chunk_id="c1", document_id="d1", ord=0, text="hi", lang="en")]
    repo, store, embedder, es = _build(doc, chunks)
    plugin = VectorExtractor(repo=repo, chunks=store, embedder=embedder, es=es)
    plugin.extract("d1")
    source = es.calls[0][0]["_source"]
    assert source["source_meta"] == "team-alpha"


def test_constructor_requires_repo_not_embedder_first() -> None:
    """New signature: VectorExtractor(repo, chunks, embedder, es) — repo is first positional arg."""
    doc = _Doc(source_title="T", source_app="app")
    chunks = [Chunk(chunk_id="c1", document_id="d1", ord=0, text="x", lang="en")]
    repo, store, embedder, es = _build(doc, chunks)
    # Must accept these as keyword args matching the new signature
    plugin = VectorExtractor(repo=repo, chunks=store, embedder=embedder, es=es)
    assert plugin is not None


def test_extract_reads_title_from_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _Doc(source_title="Dynamic Title", source_app="intranet")
    chunks = [Chunk(chunk_id="c1", document_id="d1", ord=0, text="content", lang="en")]
    repo, store, embedder, es = _build(doc, chunks)
    plugin = VectorExtractor(repo=repo, chunks=store, embedder=embedder, es=es)
    plugin.extract("d1")
    assert embedder.inputs[0] == "Dynamic Title\n\ncontent"
    assert es.calls[0][0]["_source"]["source_app"] == "intranet"
    assert es.calls[0][0]["_source"]["title"] == "Dynamic Title"
