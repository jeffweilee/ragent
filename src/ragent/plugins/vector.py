"""VectorExtractor — Phase 1 W3 (spec §2 Indexing Pipeline, plan 3.1-3.2).

Idempotency: ES bulk uses chunk_id as _id, so re-extracting the same document_id
upserts in place. Embedder/ES/chunk_store are injected (Protocol-shaped) so
unit tests stay free of network IO.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    ord: int
    text: str
    lang: str


class _Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class _ES(Protocol):
    def bulk(self, actions: list[dict[str, Any]]) -> None: ...


class VectorExtractor:
    name = "vector"
    required = True
    queue = "extract.vector"

    def __init__(
        self,
        embedder: _Embedder,
        es: _ES,
        chunk_store: dict[str, list[Chunk]],
        index: str = "chunks_v1",
    ) -> None:
        self._embedder = embedder
        self._es = es
        self._chunks = chunk_store
        self._index = index

    def extract(self, document_id: str) -> None:
        chunks = self._chunks.get(document_id, [])
        if not chunks:
            return
        vectors = self._embedder.embed([c.text for c in chunks])
        actions = [
            {
                "_op_type": "index",
                "_index": self._index,
                "_id": c.chunk_id,
                "_source": {
                    "chunk_id": c.chunk_id,
                    "document_id": c.document_id,
                    "lang": c.lang,
                    "text": c.text,
                    "embedding": v,
                },
            }
            for c, v in zip(chunks, vectors, strict=True)
        ]
        self._es.bulk(actions)

    def delete(self, document_id: str) -> None:
        chunks = self._chunks.get(document_id, [])
        actions = [{"_op_type": "delete", "_index": self._index, "_id": c.chunk_id} for c in chunks]
        if actions:
            self._es.bulk(actions)

    def health(self) -> bool:
        return self._embedder is not None and self._es is not None
