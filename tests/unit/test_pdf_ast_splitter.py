"""TDD — _PdfASTSplitter: PDF binary → Document atoms via pymupdf4llm per-page to_markdown."""

from __future__ import annotations


def _make_pdf_bytes(pages: list[str]) -> bytes:
    """Build a minimal PDF in-memory. Each string becomes one page of text."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(False)
    for text in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 10, text=text)
    return bytes(pdf.output())


def _run_splitter(data: bytes) -> list:
    from haystack.dataclasses import Document as HDoc

    from ragent.pipelines.ingest import _PdfASTSplitter

    splitter = _PdfASTSplitter()
    doc = HDoc(
        content=None,
        meta={"mime_type": "application/pdf", "document_id": "doc-pdf", "raw_bytes": data},
    )
    return splitter.run([doc])["documents"]


# ---------------------------------------------------------------------------
# Page-level atoms
# ---------------------------------------------------------------------------


def test_pdf_at_least_one_atom_per_nonempty_page():
    data = _make_pdf_bytes(["Page one content", "Page two content"])
    atoms = _run_splitter(data)
    page_numbers = {a.meta["page_number"] for a in atoms}
    assert page_numbers == {1, 2}


def test_pdf_content_contains_page_text():
    data = _make_pdf_bytes(["Hello World"])
    atoms = _run_splitter(data)
    assert len(atoms) >= 1
    assert any("Hello World" in a.content for a in atoms)


def test_pdf_raw_content_set():
    data = _make_pdf_bytes(["Test content"])
    atoms = _run_splitter(data)
    assert len(atoms) >= 1
    assert all("raw_content" in a.meta for a in atoms)
    assert any("Test content" in a.meta["raw_content"] for a in atoms)


def test_pdf_meta_passthrough():
    data = _make_pdf_bytes(["text"])
    atoms = _run_splitter(data)
    assert len(atoms) >= 1
    for atom in atoms:
        assert atom.meta["document_id"] == "doc-pdf"
        assert atom.meta["mime_type"] == "application/pdf"


def test_pdf_empty_bytes_skipped():
    """Documents with no raw_bytes payload are skipped without raising."""
    atoms = _run_splitter(b"")
    assert atoms == []


def test_pdf_empty_page_skipped():
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.add_page()  # blank page — no content
    atoms = _run_splitter(bytes(pdf.output()))
    assert atoms == []


def test_pdf_page_number_in_meta():
    data = _make_pdf_bytes(["First", "Second", "Third"])
    atoms = _run_splitter(data)
    page_numbers = {a.meta.get("page_number") for a in atoms}
    assert page_numbers == {1, 2, 3}


# ---------------------------------------------------------------------------
# to_markdown integration — splitter calls pymupdf4llm per page
# ---------------------------------------------------------------------------


def test_pdf_splitter_calls_to_markdown_per_page(monkeypatch):
    """to_markdown is called once per page with pages=[i] and use_ocr=True."""
    from unittest.mock import patch

    calls = []

    def fake_to_markdown(pdf, *, pages, use_ocr, **kwargs):
        calls.append(pages)
        return f"Page {pages[0]} content\n"

    with patch("ragent.pipelines.ingest.pymupdf4llm") as mock_module:
        mock_module.to_markdown.side_effect = fake_to_markdown
        data = _make_pdf_bytes(["Alpha", "Beta"])
        _run_splitter(data)

    assert len(calls) == 2
    assert calls[0] == [0]
    assert calls[1] == [1]


def test_pdf_empty_markdown_page_skipped(monkeypatch):
    """Pages where to_markdown returns blank markdown produce no atoms."""
    from unittest.mock import patch

    with patch("ragent.pipelines.ingest.pymupdf4llm") as mock_module:
        mock_module.to_markdown.return_value = "   "
        data = _make_pdf_bytes(["anything"])
        atoms = _run_splitter(data)

    assert atoms == []


def test_pdf_to_markdown_fallback_on_failure(monkeypatch):
    """When to_markdown raises, falls back to plain fitz text; page is still ingested."""
    from unittest.mock import patch

    with patch("ragent.pipelines.ingest.pymupdf4llm") as mock_module:
        mock_module.to_markdown.side_effect = RuntimeError("rapidocr internal error")
        data = _make_pdf_bytes(["Fallback text"])
        atoms = _run_splitter(data)

    assert len(atoms) >= 1
    assert any("Fallback text" in a.content for a in atoms)


def test_pdf_store_shrink_called_once_per_page(monkeypatch):
    """MuPDF LRU cache is evicted after every page to bound peak RSS."""
    import fitz

    shrink_calls: list[int] = []
    monkeypatch.setattr(fitz.TOOLS, "store_shrink", lambda pct: shrink_calls.append(pct))

    data = _make_pdf_bytes(["Alpha", "Beta", "Gamma"])
    _run_splitter(data)
    assert shrink_calls == [100, 100, 100]


# ---------------------------------------------------------------------------
# T-SEC.5 — Page-count cap (defends against PDF page-count expansion bombs)
# ---------------------------------------------------------------------------


def test_pdf_page_count_exceeds_cap_raises(monkeypatch):
    """A PDF whose page_count exceeds INGEST_MAX_PDF_PAGES is rejected
    BEFORE the per-page extraction loop runs."""
    import pytest

    from ragent.pipelines import ingest
    from ragent.security.archive_guard import PdfTooManyPagesError

    monkeypatch.setattr(ingest, "INGEST_MAX_PDF_PAGES", 2)
    data = _make_pdf_bytes(["A", "B", "C"])  # 3 pages > cap of 2

    with pytest.raises(PdfTooManyPagesError) as exc_info:
        _run_splitter(data)

    exc = exc_info.value
    assert exc.http_status == 413
    assert exc.error_code == "INGEST_PDF_TOO_MANY_PAGES"
    assert exc.page_count == 3
    assert exc.cap == 2


def test_pdf_page_count_at_cap_passes(monkeypatch):
    """A PDF exactly at the cap is accepted (boundary)."""
    from ragent.pipelines import ingest

    monkeypatch.setattr(ingest, "INGEST_MAX_PDF_PAGES", 3)
    data = _make_pdf_bytes(["A", "B", "C"])  # exactly 3 pages
    atoms = _run_splitter(data)
    page_numbers = {a.meta["page_number"] for a in atoms}
    assert page_numbers == {1, 2, 3}


def test_pdf_max_pages_module_default():
    """Default cap is 2000 — generous for legitimate scanned reports."""
    from ragent.security.archive_guard import INGEST_MAX_PDF_PAGES

    assert INGEST_MAX_PDF_PAGES == 2000


# ---------------------------------------------------------------------------
# T-HDR.1 — PDF margins passed to to_markdown
# ---------------------------------------------------------------------------


def test_pdf_margins_passed_to_to_markdown(monkeypatch):
    """INGEST_PDF_MARGIN_PTS is forwarded as margins=(0,v,0,v) to to_markdown."""
    from unittest.mock import patch

    from ragent.pipelines import ingest

    monkeypatch.setattr(ingest, "INGEST_PDF_MARGIN_PTS", 50.0)
    received = {}

    def fake_to_markdown(pdf, *, pages, use_ocr, margins, **kwargs):
        received["margins"] = margins
        return "content\n"

    with patch("ragent.pipelines.ingest.pymupdf4llm") as mock_module:
        mock_module.to_markdown.side_effect = fake_to_markdown
        data = _make_pdf_bytes(["text"])
        _run_splitter(data)

    assert received["margins"] == (0, 50.0, 0, 50.0)


def test_pdf_margins_default_zero():
    """Default INGEST_PDF_MARGIN_PTS is 0 (no clipping)."""
    from ragent.pipelines import ingest

    assert ingest.INGEST_PDF_MARGIN_PTS == 0.0
