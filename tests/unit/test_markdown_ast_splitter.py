"""T2v.32 — _MarkdownASTSplitter atomizes top-level blocks; never splits fences."""

from __future__ import annotations

from haystack.dataclasses import Document

from ragent.pipelines.factory import _MarkdownASTSplitter


def _run(src: str) -> list[Document]:
    return _MarkdownASTSplitter().run([Document(content=src, meta={"mime_type": "text/markdown"})])[
        "documents"
    ]


def test_fenced_code_block_kept_atomic() -> None:
    src = "# Title\n\nIntro paragraph.\n\n```py\nx = 1\ny = 2\n```\n\nTail.\n"
    atoms = _run(src)
    fence = [a for a in atoms if "```" in (a.meta.get("raw_content") or "")]
    assert len(fence) == 1
    assert "x = 1" in fence[0].meta["raw_content"]
    assert "y = 2" in fence[0].meta["raw_content"]


def test_heading_paragraph_list_each_become_atoms() -> None:
    src = "# H1\n\npara 1\n\n- a\n- b\n"
    atoms = _run(src)
    assert len(atoms) >= 3
    # Each atom carries raw_content and inherits parent meta.
    for a in atoms:
        assert a.meta.get("raw_content")
        assert a.meta.get("mime_type") == "text/markdown"


def test_content_strips_markdown_markers_but_raw_keeps_them() -> None:
    """`content` is normalized (used for embedding + BM25); `raw_content`
    keeps fences/heading hashes/emphasis for citation rendering."""
    src = "# Heading One\n\nA **bold** word and `inline` code.\n\n```py\nx = 1\n```\n"
    atoms = _run(src)
    by_kind = {("```" in a.meta["raw_content"]): a for a in atoms}
    fence = by_kind[True]
    assert "```" in fence.meta["raw_content"]
    assert "```" not in fence.content
    assert "x = 1" in fence.content

    heading = next(a for a in atoms if a.meta["raw_content"].startswith("# "))
    assert heading.content.startswith("Heading One")
    assert not heading.content.startswith("#")

    para = next(a for a in atoms if "**bold**" in a.meta["raw_content"])
    assert "**" not in para.content
    assert "bold" in para.content
    assert "`" not in para.content
    assert "inline" in para.content


def test_deterministic_across_runs() -> None:
    src = "# H\n\np\n\n```\ncode\n```\n"
    a = [d.meta["raw_content"] for d in _run(src)]
    b = [d.meta["raw_content"] for d in _run(src)]
    assert a == b
