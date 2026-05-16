"""TDD — _PdfASTSplitter: PDF binary → Document atoms (one per page)."""

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

    from ragent.pipelines.factory import _PdfASTSplitter

    splitter = _PdfASTSplitter()
    doc = HDoc(
        content=None,
        meta={"mime_type": "application/pdf", "document_id": "doc-pdf", "raw_bytes": data},
    )
    return splitter.run([doc])["documents"]


# ---------------------------------------------------------------------------
# Page-level atoms
# ---------------------------------------------------------------------------


def test_pdf_one_atom_per_page():
    data = _make_pdf_bytes(["Page one content", "Page two content"])
    atoms = _run_splitter(data)
    assert len(atoms) == 2


def test_pdf_content_contains_page_text():
    data = _make_pdf_bytes(["Hello World"])
    atoms = _run_splitter(data)
    assert len(atoms) == 1
    assert "Hello World" in atoms[0].content


def test_pdf_raw_content_set():
    data = _make_pdf_bytes(["Test content"])
    atoms = _run_splitter(data)
    assert "raw_content" in atoms[0].meta
    assert "Test content" in atoms[0].meta["raw_content"]


def test_pdf_meta_passthrough():
    data = _make_pdf_bytes(["text"])
    atoms = _run_splitter(data)
    assert atoms[0].meta["document_id"] == "doc-pdf"
    assert atoms[0].meta["mime_type"] == "application/pdf"


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
    assert len(atoms) == 3
    assert [a.meta.get("page_number") for a in atoms] == [1, 2, 3]


# ---------------------------------------------------------------------------
# _pdf_page_text — OCR branch (image-bearing pages)
# ---------------------------------------------------------------------------


def test_pdf_page_text_no_images_uses_fast_path():
    """Pages without images return plain get_text without calling OCR."""
    from unittest.mock import MagicMock

    from ragent.pipelines.factory import _pdf_page_text

    page = MagicMock()
    page.get_images.return_value = []
    page.get_text.return_value = "  Plain text  "

    result = _pdf_page_text(page)
    assert result == "Plain text"
    page.get_textpage_ocr.assert_not_called()


def test_pdf_page_text_ocr_success():
    """Pages with images use OCR; result is the OCR-extracted text."""
    from unittest.mock import MagicMock

    from ragent.pipelines.factory import _pdf_page_text

    page = MagicMock()
    page.get_images.return_value = [("xref",)]
    fake_tp = object()
    page.get_textpage_ocr.return_value = fake_tp
    page.get_text.return_value = "  OCR result  "

    result = _pdf_page_text(page)
    assert result == "OCR result"
    page.get_text.assert_called_with(textpage=fake_tp)


def test_pdf_page_text_ocr_fallback_on_failure():
    """When OCR raises (e.g. Tesseract not installed), falls back to plain get_text."""
    from unittest.mock import MagicMock

    from ragent.pipelines.factory import _pdf_page_text

    page = MagicMock()
    page.get_images.return_value = [("xref",)]
    page.get_textpage_ocr.side_effect = RuntimeError("tesseract not available")
    page.get_text.return_value = "Fallback plain text"

    result = _pdf_page_text(page)
    assert result == "Fallback plain text"
    page.get_textpage_ocr.assert_called_once()


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

    from ragent.pipelines import factory
    from ragent.security.archive_guard import PdfTooManyPagesError

    monkeypatch.setattr(factory, "INGEST_MAX_PDF_PAGES", 2)
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
    from ragent.pipelines import factory

    monkeypatch.setattr(factory, "INGEST_MAX_PDF_PAGES", 3)
    data = _make_pdf_bytes(["A", "B", "C"])  # exactly 3 pages
    atoms = _run_splitter(data)
    assert len(atoms) == 3


def test_pdf_max_pages_module_default():
    """Default cap is 2000 — generous for legitimate scanned reports."""
    from ragent.security.archive_guard import INGEST_MAX_PDF_PAGES

    assert INGEST_MAX_PDF_PAGES == 2000
