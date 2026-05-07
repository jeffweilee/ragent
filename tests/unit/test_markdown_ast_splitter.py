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


def test_deterministic_across_runs() -> None:
    src = "# H\n\np\n\n```\ncode\n```\n"
    a = [d.meta["raw_content"] for d in _run(src)]
    b = [d.meta["raw_content"] for d in _run(src)]
    assert a == b
