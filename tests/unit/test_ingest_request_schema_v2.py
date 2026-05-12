"""T2v.22 — Pydantic discriminated union for v2 ingest request (spec §3.1)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ragent.schemas.ingest import (
    FileIngestRequest,
    IngestMime,
    IngestRequest,
    InlineIngestRequest,
)

_INLINE_BASE = {
    "ingest_type": "inline",
    "source_id": "DOC-1",
    "source_app": "confluence",
    "source_title": "T",
    "mime_type": "text/markdown",
    "content": "# H1\n",
}

_FILE_BASE = {
    "ingest_type": "file",
    "source_id": "DOC-2",
    "source_app": "s3",
    "source_title": "T",
    "mime_type": "text/html",
    "minio_site": "tenant-eu-1",
    "object_key": "reports/2025.html",
}


def _adapter():
    return TypeAdapter(IngestRequest)


def test_inline_happy_path_validates():
    req = _adapter().validate_python(_INLINE_BASE)
    assert isinstance(req, InlineIngestRequest)
    assert req.ingest_type == "inline"
    assert req.content == "# H1\n"
    assert req.mime_type == IngestMime.TEXT_MARKDOWN


def test_file_happy_path_validates():
    req = _adapter().validate_python(_FILE_BASE)
    assert isinstance(req, FileIngestRequest)
    assert req.ingest_type == "file"
    assert req.minio_site == "tenant-eu-1"
    assert req.object_key == "reports/2025.html"


def test_unknown_mime_rejected():
    bad = {**_INLINE_BASE, "mime_type": "image/png"}
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_csv_mime_rejected_in_v2():
    bad = {**_INLINE_BASE, "mime_type": "text/csv"}
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_inline_missing_content_rejected():
    bad = dict(_INLINE_BASE)
    del bad["content"]
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_inline_empty_content_rejected():
    bad = {**_INLINE_BASE, "content": ""}
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_file_missing_object_key_rejected():
    bad = dict(_FILE_BASE)
    del bad["object_key"]
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_file_missing_minio_site_rejected():
    bad = dict(_FILE_BASE)
    del bad["minio_site"]
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_missing_source_title_rejected():
    bad = dict(_INLINE_BASE)
    del bad["source_title"]
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_unknown_ingest_type_rejected():
    bad = {**_INLINE_BASE, "ingest_type": "ftp"}
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_source_url_max_length_2048():
    long_url = "https://x/" + "a" * 2050
    bad = {**_INLINE_BASE, "source_url": long_url}
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)


def test_source_url_accepts_under_cap():
    ok = {**_INLINE_BASE, "source_url": "https://wiki/page"}
    req = _adapter().validate_python(ok)
    assert req.source_url == "https://wiki/page"


def test_source_meta_optional():
    req = _adapter().validate_python(_INLINE_BASE)
    assert req.source_meta is None


def test_ingest_mime_enum_values():
    assert IngestMime.TEXT_PLAIN.value == "text/plain"
    assert IngestMime.TEXT_MARKDOWN.value == "text/markdown"
    assert IngestMime.TEXT_HTML.value == "text/html"
    # CSV is intentionally NOT in v2 enum
    assert "text/csv" not in {m.value for m in IngestMime}


# ---------------------------------------------------------------------------
# Alias support: short names normalise to full MIME strings
# ---------------------------------------------------------------------------

_FILE_PPTX_BASE = {
    "ingest_type": "file",
    "source_id": "DOC-3",
    "source_app": "upload-cli",
    "source_title": "Slides",
    "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "minio_site": "corp",
    "object_key": "slides.pptx",
}

_FILE_DOCX_BASE = {
    **_FILE_PPTX_BASE,
    "source_id": "DOC-4",
    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "object_key": "report.docx",
}


def test_pptx_alias_normalises_to_full_mime():
    req = _adapter().validate_python({**_FILE_PPTX_BASE, "mime_type": "pptx"})
    assert req.mime_type == IngestMime.PPTX


def test_docx_alias_normalises_to_full_mime():
    req = _adapter().validate_python({**_FILE_DOCX_BASE, "mime_type": "docx"})
    assert req.mime_type == IngestMime.DOCX


def test_pptx_alias_case_insensitive():
    req = _adapter().validate_python({**_FILE_PPTX_BASE, "mime_type": "PPTX"})
    assert req.mime_type == IngestMime.PPTX


def test_file_pptx_full_mime_accepted():
    req = _adapter().validate_python(_FILE_PPTX_BASE)
    assert isinstance(req, FileIngestRequest)
    assert req.mime_type == IngestMime.PPTX


def test_file_docx_full_mime_accepted():
    req = _adapter().validate_python(_FILE_DOCX_BASE)
    assert isinstance(req, FileIngestRequest)
    assert req.mime_type == IngestMime.DOCX


# ---------------------------------------------------------------------------
# Binary MIME types are rejected for ingest_type=inline
# ---------------------------------------------------------------------------


def test_inline_pptx_mime_rejected():
    bad = {
        **_INLINE_BASE,
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    with pytest.raises(ValidationError) as exc_info:
        _adapter().validate_python(bad)
    assert "binary" in str(exc_info.value).lower()


def test_inline_docx_mime_rejected():
    bad = {
        **_INLINE_BASE,
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    with pytest.raises(ValidationError) as exc_info:
        _adapter().validate_python(bad)
    assert "binary" in str(exc_info.value).lower()


def test_inline_pptx_alias_also_rejected():
    bad = {**_INLINE_BASE, "mime_type": "pptx"}
    with pytest.raises(ValidationError):
        _adapter().validate_python(bad)
