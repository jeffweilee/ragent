"""T2v.23 — Pydantic models for v2 ingest API (spec §3.1, §4.1).

Discriminated union over `ingest_type`:
  - inline: `content` is in the JSON body.
  - file:   bytes live in caller-supplied `(minio_site, object_key)`.

`mime_type` is a closed enum (text/plain | text/markdown | text/html | docx | pptx | pdf).
Short aliases "pptx", "docx", and "pdf" are accepted and normalised to their full IANA MIME
strings at validation time.  Binary MIME types (DOCX, PPTX, PDF) are rejected for
ingest_type "inline" because the `content` field is a UTF-8 string — use
POST /ingest/v1/upload for binary files.
`minio_site` is validated against the runtime registry at the service layer
(not here) so the schema stays config-free.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ragent.utility.compat import StrEnum

SOURCE_URL_MAX = 2048
SOURCE_META_MAX = 1024


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class IngestCreatedResponse(BaseModel):
    document_id: str


class IngestListItem(BaseModel):
    document_id: str
    status: str
    source_id: str
    source_app: str
    source_title: str
    updated_at: str | None


class IngestListResponse(BaseModel):
    items: list[IngestListItem]
    next_cursor: str | None


class IngestDetailResponse(BaseModel):
    document_id: str
    status: str
    attempt: int
    updated_at: str | None
    ingest_type: str
    minio_site: str | None
    source_id: str
    source_app: str
    source_title: str
    source_meta: str | None
    source_url: str | None
    error_code: str | None
    error_reason: str | None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class IngestMime(StrEnum):
    TEXT_PLAIN = "text/plain"
    TEXT_MARKDOWN = "text/markdown"
    TEXT_HTML = "text/html"
    CSV = "text/csv"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    PDF = "application/pdf"

    @classmethod
    def _missing_(cls, value: object) -> IngestMime | None:
        # RFC 2045 §5.1: media-type matching is case-insensitive.
        v = str(value).lower()
        if v in _MIME_ALIASES:
            return _MIME_ALIASES[v]
        return next((m for m in cls if m.value == v), None)


_MIME_ALIASES: dict[str, IngestMime] = {
    "docx": IngestMime.DOCX,
    "pptx": IngestMime.PPTX,
    "pdf": IngestMime.PDF,
}

BINARY_MIMES: frozenset[IngestMime] = frozenset({IngestMime.DOCX, IngestMime.PPTX, IngestMime.PDF})

# File extensions implied by each IngestMime. The unprotect upstream uses the
# multipart `filename` to route to the right parser (xxx.pptx → PPTX path),
# so the worker appends the canonical extension before calling unprotect.
MIME_EXTENSIONS: dict[IngestMime, str] = {
    IngestMime.TEXT_PLAIN: "txt",
    IngestMime.TEXT_MARKDOWN: "md",
    IngestMime.TEXT_HTML: "html",
    IngestMime.CSV: "csv",
    IngestMime.DOCX: "docx",
    IngestMime.PPTX: "pptx",
    IngestMime.PDF: "pdf",
}


class _IngestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    source_app: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    source_meta: str | None = Field(default=None, max_length=SOURCE_META_MAX)
    source_url: str | None = Field(default=None, max_length=SOURCE_URL_MAX)
    mime_type: IngestMime


class InlineIngestRequest(_IngestBase):
    ingest_type: Literal["inline"]
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_binary_mime(self) -> InlineIngestRequest:
        if self.mime_type in BINARY_MIMES:
            raise ValueError(
                f"{self.mime_type!r} is a binary format; "
                "binary MIME types require ingest_type='file' or POST /ingest/v1/upload"
            )
        return self


class FileIngestRequest(_IngestBase):
    ingest_type: Literal["file"]
    minio_site: str = Field(min_length=1)
    object_key: str = Field(min_length=1)


IngestRequest = Annotated[
    InlineIngestRequest | FileIngestRequest,
    Field(discriminator="ingest_type"),
]
