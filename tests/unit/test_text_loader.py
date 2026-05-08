"""T2v.30 — _TextLoader builds Document(content=str) from inline content + meta."""

from __future__ import annotations

from ragent.pipelines.factory import _TextLoader


def test_loader_builds_single_document_with_meta() -> None:
    out = _TextLoader().run(
        content="hello",
        mime_type="text/markdown",
        document_id="DOC-1",
        source_url="https://x/y",
        source_title="t",
        source_app="app",
        source_meta="ws",
    )
    docs = out["documents"]
    assert len(docs) == 1
    d = docs[0]
    assert d.content == "hello"
    assert d.meta == {
        "mime_type": "text/markdown",
        "document_id": "DOC-1",
        "source_url": "https://x/y",
        "source_title": "t",
        "source_app": "app",
        "source_meta": "ws",
    }


def test_loader_omits_unset_optional_meta() -> None:
    out = _TextLoader().run(content="hi", mime_type="text/plain")
    d = out["documents"][0]
    assert d.meta == {"mime_type": "text/plain"}
