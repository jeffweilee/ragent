"""Schemas for chat attachments (T-CAT.2, T-CAT.3).

AttachmentMime is schema-isolated from IngestMime; the two can evolve
independently even though the values match today (§3.4.9).
"""

from __future__ import annotations

from ragent.utility.compat import StrEnum


class AttachmentMime(StrEnum):
    """MIME types allowed for chat attachments."""

    TEXT_PLAIN = "text/plain"
    TEXT_MARKDOWN = "text/markdown"
    TEXT_HTML = "text/html"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    PDF = "application/pdf"

    @classmethod
    def _missing_(cls, value: object) -> AttachmentMime | None:
        """Case-insensitive MIME type lookup."""
        v = str(value).lower()
        return next((m for m in cls if m.value == v), None)

    @classmethod
    def resolve_from_extension(cls, ext: str) -> AttachmentMime | None:
        """Resolve MIME type from file extension (fallback for browser Content-Type).

        Handles unreliable browser-supplied Content-Type by attempting to
        resolve the true MIME type from the file extension.
        """
        ext_lower = ext.lower().lstrip(".")
        return _EXTENSION_TO_MIME.get(ext_lower)


MIME_EXTENSIONS: dict[AttachmentMime, str] = {
    AttachmentMime.TEXT_PLAIN: "txt",
    AttachmentMime.TEXT_MARKDOWN: "md",
    AttachmentMime.TEXT_HTML: "html",
    AttachmentMime.DOCX: "docx",
    AttachmentMime.PPTX: "pptx",
    AttachmentMime.PDF: "pdf",
}

_EXTENSION_TO_MIME: dict[str, AttachmentMime] = {v: k for k, v in MIME_EXTENSIONS.items()}

UNPROTECT_MIMES: frozenset[AttachmentMime] = frozenset(
    {
        AttachmentMime.PDF,
        AttachmentMime.DOCX,
        AttachmentMime.PPTX,
    }
)

# Pins the relationship between an uploaded attachment's MIME and the
# content_type its pipeline's AST artifact is rendered as (docs/spec/
# chat_attachments.md §2.1). A dict, not branching in the service — adding a
# format only adds an entry here (OCP); nothing downstream needs to change.
ARTIFACT_CONTENT_TYPE: dict[AttachmentMime, str] = {
    AttachmentMime.TEXT_PLAIN: "text/markdown",
    AttachmentMime.TEXT_MARKDOWN: "text/markdown",
    AttachmentMime.TEXT_HTML: "text/markdown",
    AttachmentMime.DOCX: "text/markdown",
    AttachmentMime.PPTX: "text/markdown",
    AttachmentMime.PDF: "text/markdown",
}
