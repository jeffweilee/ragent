"""T2v.36 — _BudgetChunker is mime-agnostic 1000/1500/100; preserves raw_content."""

from __future__ import annotations

from haystack.dataclasses import Document

from ragent.pipelines.factory import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    CHUNK_TARGET_CHARS,
    _BudgetChunker,
)


def _atom(text: str, raw: str | None = None, document_id: str = "DOC-1") -> Document:
    return Document(
        content=text,
        meta={"raw_content": raw if raw is not None else text, "document_id": document_id},
    )


def test_pack_below_target_yields_single_chunk() -> None:
    atoms = [_atom("a" * 100), _atom("b" * 100), _atom("c" * 100)]
    out = _BudgetChunker().run(atoms)["documents"]
    assert len(out) == 1
    assert out[0].meta["split_id"] == 0
    assert out[0].meta["raw_content"]


def test_pack_overflows_target_creates_multiple_chunks() -> None:
    # Three atoms ~600 chars each → one chunk fits at most one full atom + overflow.
    atoms = [_atom("x" * 600) for _ in range(3)]
    out = _BudgetChunker().run(atoms)["documents"]
    assert len(out) >= 2
    for d in out:
        assert len(d.content) <= CHUNK_TARGET_CHARS + CHUNK_OVERLAP_CHARS


def test_atom_over_max_is_hard_split_with_overlap() -> None:
    big = _atom("y" * (CHUNK_MAX_CHARS + 500))
    out = _BudgetChunker().run([big])["documents"]
    assert len(out) >= 2
    for d in out:
        assert len(d.content) <= CHUNK_TARGET_CHARS


def test_split_id_resets_per_document_id() -> None:
    a = [_atom("a" * 600, document_id="DOC-1") for _ in range(3)]
    b = [_atom("b" * 600, document_id="DOC-2") for _ in range(3)]
    out = _BudgetChunker().run(a + b)["documents"]
    by_doc: dict[str, list[int]] = {}
    for d in out:
        by_doc.setdefault(d.meta["document_id"], []).append(d.meta["split_id"])
    for split_ids in by_doc.values():
        assert split_ids[0] == 0
        assert split_ids == sorted(split_ids)


def test_raw_content_preserved_through_packing() -> None:
    atoms = [
        _atom("text-a", raw="```a```"),
        _atom("text-b", raw="```b```"),
    ]
    out = _BudgetChunker().run(atoms)["documents"]
    assert len(out) == 1
    raw = out[0].meta["raw_content"]
    assert "```a```" in raw
    assert "```b```" in raw
