"""T2v.38 — Mime-aware splitter dispatches per meta['mime_type'].

Unknown MIME raises IngestStepError(error_code=PIPELINE_UNROUTABLE).
"""

from __future__ import annotations

import pytest
from haystack.dataclasses import Document

from ragent.pipelines.factory import _MimeAwareSplitter
from ragent.pipelines.observability import IngestStepError


def test_plain_routes_to_document_splitter() -> None:
    out = _MimeAwareSplitter().run(
        [Document(content="hello world.", meta={"mime_type": "text/plain"})]
    )["documents"]
    assert len(out) >= 1
    for a in out:
        assert a.meta.get("raw_content")  # plain fallback uses content as raw


def test_markdown_routes_to_markdown_ast_splitter() -> None:
    out = _MimeAwareSplitter().run(
        [Document(content="# H\n\np\n\n```\nx=1\n```", meta={"mime_type": "text/markdown"})]
    )["documents"]
    raws = "".join(a.meta.get("raw_content", "") for a in out)
    assert "```" in raws


def test_html_routes_to_html_ast_splitter() -> None:
    out = _MimeAwareSplitter().run(
        [Document(content="<p>hello</p><script>x=1</script>", meta={"mime_type": "text/html"})]
    )["documents"]
    raws = " ".join(a.meta.get("raw_content", "") for a in out)
    assert "x=1" not in raws


def test_unknown_mime_raises_pipeline_unroutable() -> None:
    with pytest.raises(IngestStepError) as exc:
        _MimeAwareSplitter().run([Document(content="x", meta={"mime_type": "application/pdf"})])
    assert exc.value.error_code == "PIPELINE_UNROUTABLE"
