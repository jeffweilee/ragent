"""T2v.23 — Pydantic models for v2 ingest API (spec §3.1).

Discriminated union over `ingest_type`:
  - inline: `content` is in the JSON body.
  - file:   bytes live in caller-supplied `(minio_site, object_key)`.

`mime_type` is a closed enum (text/plain | text/markdown | text/html | docx | pptx).
Short aliases "pptx" and "docx" are accepted and normalised to their full IANA MIME
strings at validation time.  Binary MIME types (DOCX, PPTX) are rejected for
ingest_type "inline" because the `content` field is a UTF-8 string — use
POST /ingest/v1/upload for binary files.
`minio_site` is validated against the runtime registry at the service layer
(not here) so the schema stays config-free.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCE_URL_MAX = 2048
SOURCE_META_MAX = 1024


class IngestMime(StrEnum):
    TEXT_PLAIN = "text/plain"
    TEXT_MARKDOWN = "text/markdown"
    TEXT_HTML = "text/html"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

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
}

BINARY_MIMES: frozenset[IngestMime] = frozenset({IngestMime.DOCX, IngestMime.PPTX})


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
