from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    ord: int
    text: str
    lang: str


class _Repo(Protocol):
    def get(self, document_id: str) -> Any: ...


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
        repo: _Repo,
        chunks: dict[str, list[Chunk]],
        embedder: _Embedder,
        es: _ES,
        index: str = "chunks_v1",
    ) -> None:
        self._repo = repo
        self._chunks = chunks
        self._embedder = embedder
        self._es = es
        self._index = index

    def extract(self, document_id: str) -> None:
        chunk_list = self._chunks.get(document_id, [])
        if not chunk_list:
            return
        doc = self._repo.get(document_id)
        title = doc.source_title
        inputs = [f"{title}\n\n{c.text}" for c in chunk_list]
        vectors = self._embedder.embed(inputs)
        actions = []
        for c, v in zip(chunk_list, vectors, strict=True):
            source: dict[str, Any] = {
                "chunk_id": c.chunk_id,
                "document_id": c.document_id,
                "lang": c.lang,
                "title": title,
                "text": c.text,
                "embedding": v,
                "source_app": doc.source_app,
            }
            if doc.source_workspace is not None:
                source["source_workspace"] = doc.source_workspace
            actions.append(
                {"_op_type": "index", "_index": self._index, "_id": c.chunk_id, "_source": source}
            )
        self._es.bulk(actions)

    def delete(self, document_id: str) -> None:
        chunk_list = self._chunks.get(document_id, [])
        actions = [
            {"_op_type": "delete", "_index": self._index, "_id": c.chunk_id} for c in chunk_list
        ]
        if actions:
            self._es.bulk(actions)

    def health(self) -> bool:
        return self._embedder is not None and self._es is not None
