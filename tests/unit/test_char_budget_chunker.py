"""Tests for _CharBudgetChunker — unified per-language chunking (T3.2/B1)."""

from __future__ import annotations

from haystack.dataclasses import Document

from ragent.pipelines.factory import _CharBudgetChunker


def test_en_paragraph_packs_to_target_with_overlap() -> None:
    sentences = [f"Sentence number {i} talks about topic {i}." for i in range(80)]
    text = " ".join(sentences)
    docs = _CharBudgetChunker().run(documents=[Document(content=text)])["documents"]

    assert len(docs) >= 2
    for doc in docs:
        assert len(doc.content) <= 2200  # target 2000 + atom slack
    # overlap: consecutive chunks share at least one sentence boundary worth of text
    for prev, curr in zip(docs, docs[1:], strict=False):
        tail = prev.content[-300:]
        head = curr.content[:300]
        assert any(s in head for s in tail.split(". ") if len(s) > 15)


def test_cjk_paragraph_uses_500_char_budget() -> None:
    cjk_sentence = "这是一个中文句子。"  # 9 chars including 。
    text = cjk_sentence * 200  # 1800 chars
    doc = Document(content=text)
    docs = _CharBudgetChunker().run(documents=[doc])["documents"]

    assert len(docs) >= 3
    for chunk in docs:
        assert len(chunk.content) <= 600  # target 500 + atom slack


def test_csv_uses_lines_no_overlap() -> None:
    rows = [f"col_a_{i},col_b_{i},col_c_value_{i}" for i in range(200)]
    text = "\n".join(rows)
    doc = Document(content=text, meta={"content_type": "text/csv"})
    docs = _CharBudgetChunker().run(documents=[doc])["documents"]

    assert len(docs) >= 2
    for chunk in docs:
        assert len(chunk.content) <= 2200
    # no overlap: no row should appear in two consecutive chunks
    for prev, curr in zip(docs, docs[1:], strict=False):
        prev_rows = set(prev.content.splitlines())
        curr_rows = set(curr.content.splitlines())
        assert not (prev_rows & curr_rows)


def test_oversized_atom_hard_split_with_overlap() -> None:
    # Single 5000-char "sentence" with no terminators
    text = "x" * 5000
    docs = _CharBudgetChunker().run(documents=[Document(content=text)])["documents"]

    assert len(docs) >= 3
    for chunk in docs:
        assert len(chunk.content) <= 2000
    # 200-char overlap on hard splits
    for prev, curr in zip(docs, docs[1:], strict=False):
        assert prev.content[-200:] == curr.content[:200]


def test_unknown_or_garbled_language_defaults_to_en() -> None:
    text = "??? !!! ###" * 50
    docs = _CharBudgetChunker().run(documents=[Document(content=text)])["documents"]
    # Should not crash; emits at least one chunk
    assert len(docs) >= 1


def test_empty_document_emits_no_chunks() -> None:
    docs = _CharBudgetChunker().run(documents=[Document(content="")])["documents"]
    assert docs == []


def test_metadata_preserved_and_split_id_monotonic() -> None:
    text = ". ".join(f"sentence {i}" for i in range(500))
    doc = Document(content=text, meta={"document_id": "doc-1", "extra": "keep"})
    docs = _CharBudgetChunker().run(documents=[doc])["documents"]

    assert len(docs) >= 2
    for i, chunk in enumerate(docs):
        assert chunk.meta["document_id"] == "doc-1"
        assert chunk.meta["extra"] == "keep"
        assert chunk.meta["split_id"] == i
        assert "split_idx_start" in chunk.meta


def test_french_uses_en_profile() -> None:
    # French detected by langdetect; should pack into ~2000-char chunks
    text = (
        "Le chat noir est assis sur la table. "
        "Il regarde par la fenêtre avec attention. "
        "Le soleil brille dans le ciel bleu et clair. "
    ) * 60
    docs = _CharBudgetChunker().run(documents=[Document(content=text)])["documents"]

    assert len(docs) >= 2
    # EN profile target is 2000, so most chunks should be substantially > 500
    big_chunks = [d for d in docs if len(d.content) > 800]
    assert big_chunks, "French should use the EN (2000-char) profile, not the CJK one"
