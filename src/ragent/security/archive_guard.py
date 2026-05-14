"""Zip-archive preflight for DOCX/PPTX uploads.

Reads only the zip central directory (`ZipInfo.file_size` is the
declared uncompressed size — no inflation occurs).  Rejects:

* `len(infolist()) > INGEST_MAX_ARCHIVE_MEMBERS` — millions-of-tiny-files bomb.
* declared / raw size ratio > `INGEST_MAX_ARCHIVE_RATIO` — classic 42.zip.
* `sum(file_size) > INGEST_MAX_ARCHIVE_EXPANDED_BYTES` — defeats padded-input
  bypass of the ratio check.
* any single member's `file_size` > `INGEST_MAX_ARCHIVE_EXPANDED_BYTES` —
  defeats "one giant + many small" bomb shape.
* member name containing `..` segment or starting with `/` — path traversal.

Failure raises `ArchiveBombError(http_status=413, error_code='INGEST_ARCHIVE_UNSAFE')`
carrying a `reason` tag for metrics / logs.
"""

from __future__ import annotations

import io
import zipfile
from enum import StrEnum
from typing import Final

from ragent.bootstrap.metrics import record_ingest_rejection
from ragent.errors.codes import HttpErrorCode
from ragent.utility.env import int_env

INGEST_MAX_ARCHIVE_MEMBERS: Final[int] = int_env("INGEST_MAX_ARCHIVE_MEMBERS", 5000)
INGEST_MAX_ARCHIVE_RATIO: Final[int] = int_env("INGEST_MAX_ARCHIVE_RATIO", 100)
INGEST_MAX_ARCHIVE_EXPANDED_BYTES: Final[int] = int_env(
    "INGEST_MAX_ARCHIVE_EXPANDED_BYTES", 524288000
)
INGEST_MAX_PDF_PAGES: Final[int] = int_env("INGEST_MAX_PDF_PAGES", 2000)


class ArchiveBombReason(StrEnum):
    """Closed label set; feeds Prometheus `ragent_ingest_rejected_total{reason}` (T-SEC.7)."""

    INVALID = "invalid"
    MEMBERS = "members"
    TRAVERSAL = "traversal"
    PER_MEMBER = "per_member"
    EXPANDED = "expanded"
    RATIO = "ratio"


class ArchiveBombError(Exception):
    """Zip preflight rejected the archive."""

    http_status: int = 413
    error_code: str = HttpErrorCode.INGEST_ARCHIVE_UNSAFE

    def __init__(self, reason: ArchiveBombReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        # Emit at construction time so the six raise-sites stay 1-line and the
        # closed `ArchiveBombReason` enum is the single source of truth for
        # both the exception's `reason` attribute and the metric label.
        record_ingest_rejection(reason.value)


def _is_traversal(name: str) -> bool:
    if name.startswith("/"):
        return True
    return any(segment == ".." for segment in name.replace("\\", "/").split("/"))


def assert_safe_zip(
    raw: bytes,
    *,
    max_members: int = INGEST_MAX_ARCHIVE_MEMBERS,
    max_ratio: int = INGEST_MAX_ARCHIVE_RATIO,
    max_expanded: int = INGEST_MAX_ARCHIVE_EXPANDED_BYTES,
) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ArchiveBombError(ArchiveBombReason.INVALID, f"not a valid zip: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > max_members:
            raise ArchiveBombError(ArchiveBombReason.MEMBERS, f"{len(infos)} > {max_members}")

        total = 0
        for info in infos:
            if _is_traversal(info.filename):
                raise ArchiveBombError(ArchiveBombReason.TRAVERSAL, info.filename)
            if info.file_size > max_expanded:
                raise ArchiveBombError(
                    ArchiveBombReason.PER_MEMBER,
                    f"{info.filename}: {info.file_size} > {max_expanded}",
                )
            total += info.file_size

        if total > max_expanded:
            raise ArchiveBombError(ArchiveBombReason.EXPANDED, f"{total} > {max_expanded}")

        raw_size = max(len(raw), 1)
        if total // raw_size > max_ratio:
            raise ArchiveBombError(ArchiveBombReason.RATIO, f"{total}/{raw_size} > {max_ratio}")


class PdfTooManyPagesError(Exception):
    """PDF page count exceeds the configured cap."""

    http_status: int = 413
    error_code: str = HttpErrorCode.INGEST_PDF_TOO_MANY_PAGES

    def __init__(self, page_count: int, cap: int) -> None:
        super().__init__(f"PDF has {page_count} pages, cap is {cap}")
        self.page_count = page_count
        self.cap = cap
        record_ingest_rejection("pdf_pages")


def assert_safe_pdf_page_count(page_count: int, *, max_pages: int) -> None:
    """Raise PdfTooManyPagesError when page_count > max_pages.

    Called from `_PdfASTSplitter.run` immediately after `fitz.open(...)` and
    before the per-page extraction loop, bounding worst-case work to
    `max_pages` * per-page OCR cost.  `max_pages` is required (no default)
    so the caller's module-level constant is the single source of truth —
    pinning that to a `int_env(...)` default here would evaluate at import
    time and shadow runtime monkeypatching in tests.
    """
    if page_count > max_pages:
        raise PdfTooManyPagesError(page_count, max_pages)
