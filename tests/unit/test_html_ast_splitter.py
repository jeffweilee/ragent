"""T2v.34 — _HtmlASTSplitter drops boilerplate, atomizes block elements."""

from __future__ import annotations

from haystack.dataclasses import Document

from ragent.pipelines.factory import _HtmlASTSplitter


def _run(html: str) -> list[Document]:
    return _HtmlASTSplitter().run([Document(content=html, meta={"mime_type": "text/html"})])[
        "documents"
    ]


def test_drops_script_style_nav_aside_footer_header_when_top_level() -> None:
    html = """
        <html><body>
          <nav>menu</nav>
          <header>head</header>
          <script>x=1</script>
          <style>.a {}</style>
          <p>real content</p>
          <footer>foo</footer>
        </body></html>
    """
    atoms = _run(html)
    texts = [a.content for a in atoms]
    raws = [a.meta["raw_content"] for a in atoms]
    assert any("real content" in t for t in texts)
    for body in texts + raws:
        assert "x=1" not in body
        assert "menu" not in body
        assert "foo" not in body


def test_pre_block_atomic() -> None:
    html = "<html><body><pre>line1\nline2</pre><p>after</p></body></html>"
    atoms = _run(html)
    pre = [a for a in atoms if "<pre>" in a.meta["raw_content"]]
    assert len(pre) == 1
    assert "line1" in pre[0].meta["raw_content"]
    assert "line2" in pre[0].meta["raw_content"]


def test_deterministic_across_runs() -> None:
    html = "<h1>x</h1><p>y</p><pre>z</pre>"
    a = [d.meta["raw_content"] for d in _run(html)]
    b = [d.meta["raw_content"] for d in _run(html)]
    assert a == b
