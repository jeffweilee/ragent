"""Magic-byte validation for binary upload MIMEs.

Binary MIME types carry a fixed file signature in their first bytes.
Verifying the signature before the bytes reach python-docx / python-pptx /
fitz defeats `Content-Type` spoofing — a `.exe` uploaded with
`mime_type=pdf` is rejected here instead of crashing the parser.

Text MIME types have no fixed signature and are not checked.
"""

from __future__ import annotations

from ragent.errors.codes import HttpErrorCode
from ragent.schemas.ingest import IngestMime

_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"

_MIME_SIGNATURES: dict[IngestMime, bytes] = {
    IngestMime.DOCX: _ZIP_MAGIC,
    IngestMime.PPTX: _ZIP_MAGIC,
    IngestMime.PDF: _PDF_MAGIC,
}


class MagicByteMismatchError(Exception):
    """Declared MIME does not match the file's leading bytes."""

    http_status: int = 415
    error_code: str = HttpErrorCode.INGEST_MAGIC_MISMATCH

    def __init__(self, mime: IngestMime, *, expected: bytes, got_prefix: bytes) -> None:
        super().__init__(
            f"{mime.value} requires signature {expected!r}; got {got_prefix!r}"
        )
        self.mime = mime


def assert_magic_byte(mime: IngestMime, raw: bytes) -> None:
    """Raise MagicByteMismatchError when raw's prefix does not match mime's signature.

    No-op for MIME types with no fixed signature (text/*).
    """
    expected = _MIME_SIGNATURES.get(mime)
    if expected is None:
        return
    if not raw.startswith(expected):
        raise MagicByteMismatchError(mime, expected=expected, got_prefix=raw[: len(expected)])
