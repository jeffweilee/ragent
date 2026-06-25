"""Tests for attachment schema (T-CAT.2)."""

from ragent.schemas.attachments import AttachmentMime, MIME_EXTENSIONS


def test_attachment_mime_enum_has_six_values():
    """T-CAT.2: AttachmentMime must have the same six values as IngestMime."""
    assert len(AttachmentMime) == 6


def test_attachment_mime_values():
    """Verify all six MIME types are present."""
    assert AttachmentMime.TEXT_PLAIN == "text/plain"
    assert AttachmentMime.TEXT_MARKDOWN == "text/markdown"
    assert AttachmentMime.TEXT_HTML == "text/html"
    assert (
        AttachmentMime.DOCX
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert (
        AttachmentMime.PPTX
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert AttachmentMime.PDF == "application/pdf"


def test_mime_extensions_mapping_complete():
    """All AttachmentMime values must have an extension mapping."""
    assert len(MIME_EXTENSIONS) == 6
    for mime in AttachmentMime:
        assert mime in MIME_EXTENSIONS


def test_extension_fallback_resolve():
    """Extension fallback resolves MIME from file extension when needed."""
    # Example: browser sends "application/octet-stream" for a .pdf file
    # Extension fallback should resolve to AttachmentMime.PDF
    assert AttachmentMime.resolve_from_extension("pdf") == AttachmentMime.PDF
    assert AttachmentMime.resolve_from_extension("docx") == AttachmentMime.DOCX
    assert AttachmentMime.resolve_from_extension("pptx") == AttachmentMime.PPTX
    assert AttachmentMime.resolve_from_extension("txt") == AttachmentMime.TEXT_PLAIN
    assert AttachmentMime.resolve_from_extension("md") == AttachmentMime.TEXT_MARKDOWN
    assert AttachmentMime.resolve_from_extension("html") == AttachmentMime.TEXT_HTML


def test_extension_fallback_case_insensitive():
    """Extension fallback must be case-insensitive."""
    assert AttachmentMime.resolve_from_extension("PDF") == AttachmentMime.PDF
    assert AttachmentMime.resolve_from_extension("Pdf") == AttachmentMime.PDF
    assert AttachmentMime.resolve_from_extension("DOCX") == AttachmentMime.DOCX


def test_extension_fallback_unknown_extension():
    """Unknown extension should return None."""
    assert AttachmentMime.resolve_from_extension("unknown") is None
    assert AttachmentMime.resolve_from_extension("xyz") is None


def test_attachment_mime_case_insensitive_lookup():
    """AttachmentMime should be case-insensitive for MIME type matching."""
    # The _missing_ method should handle case-insensitive lookups
    assert AttachmentMime("text/plain") == AttachmentMime.TEXT_PLAIN
    assert AttachmentMime("TEXT/PLAIN") == AttachmentMime.TEXT_PLAIN
    assert AttachmentMime("application/pdf") == AttachmentMime.PDF
    assert AttachmentMime("APPLICATION/PDF") == AttachmentMime.PDF
