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


# ---------------------------------------------------------------------------
# Batch mode — large-document OOM guard
# ---------------------------------------------------------------------------


def test_pdf_batch_mode_processes_all_pages(monkeypatch):
    """With a zero page threshold, batch step=PDF_BATCH_PAGES; all pages are emitted."""
    import ragent.pipelines.factory as factory_mod

    monkeypatch.setattr(factory_mod, "PDF_PAGE_BATCH_THRESHOLD", 0)
    monkeypatch.setattr(factory_mod, "PDF_BATCH_PAGES", 1)

    data = _make_pdf_bytes(["Alpha", "Beta", "Gamma"])
    atoms = _run_splitter(data)
    assert len(atoms) == 3
    assert [a.meta.get("page_number") for a in atoms] == [1, 2, 3]
