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


def test_raw_overlap_is_atom_aligned_not_char_sliced() -> None:
    """Naive `raw[-overlap:]` would cut through HTML tags / Markdown
    markers. raw_content must be atom-aligned: chunks after a flush start
    raw fresh at the next atom boundary so markup stays well-formed."""
    # Three atoms, each ~600 chars text, raw is wrapped HTML-like markup.
    atoms = [
        _atom("aaaa" * 150, raw="<p>" + "aaaa" * 150 + "</p>"),
        _atom("bbbb" * 150, raw="<p>" + "bbbb" * 150 + "</p>"),
        _atom("cccc" * 150, raw="<p>" + "cccc" * 150 + "</p>"),
    ]
    out = _BudgetChunker().run(atoms)["documents"]
    # At least the second chunk's raw must NOT start with a sliced tag
    # fragment — every raw_content begins at an atom boundary (`<p>` or
    # empty join), never in the middle of a tag like `aaa</p>`.
    for d in out[1:]:
        raw = d.meta["raw_content"]
        # raw must not start with a closing-tag fragment from the prior
        # atom — the atom-alignment guarantee.
        assert not raw.lstrip().startswith("</")


def test_hard_split_does_not_duplicate_full_raw() -> None:
    """When raw is much larger than text (HTML/markdown atoms), hard-
    splitting a > MAX atom must not duplicate the full raw across every
    output piece."""
    big_text = "x" * (CHUNK_MAX_CHARS + 800)
    big_raw = "<table>" + ("<tr><td>cell</td></tr>" * 200) + "</table>"
    atom = _atom(big_text, raw=big_raw)
    out = _BudgetChunker().run([atom])["documents"]
    assert len(out) >= 2
    # No piece may carry the entire 5KB+ raw blob.
    for d in out:
        assert len(d.meta["raw_content"]) <= CHUNK_TARGET_CHARS + 1


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


def test_hard_split_caps_pieces_per_atom(monkeypatch) -> None:
    """Pathological config (overlap >= target) must not produce unbounded chunks."""
    import pytest

    from ragent.pipelines import factory as fac

    monkeypatch.setattr(fac, "CHUNK_TARGET_CHARS", 50)
    monkeypatch.setattr(fac, "CHUNK_MAX_CHARS", 75)
    monkeypatch.setattr(fac, "CHUNK_OVERLAP_CHARS", 49)
    monkeypatch.setattr(fac, "CHUNK_MAX_PIECES_PER_ATOM", 16)

    from ragent.pipelines.observability import IngestStepError

    big = _atom("z" * 10_000)
    with pytest.raises(IngestStepError) as exc:
        fac._BudgetChunker().run([big])
    assert exc.value.error_code == "CHUNK_BUDGET_EXCEEDED"
