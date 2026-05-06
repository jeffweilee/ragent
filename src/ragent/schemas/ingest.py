"""T2v.23 — Pydantic models for v2 ingest API (spec §3.1).

Discriminated union over `ingest_type`:
  - inline: `content` is in the JSON body.
  - file:   bytes live in caller-supplied `(minio_site, object_key)`.

`content_type` is a closed enum (text/plain | text/markdown | text/html).
`minio_site` is validated against the runtime registry at the service layer
(not here) so the schema stays config-free.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SOURCE_URL_MAX = 2048


class IngestMime(StrEnum):
    TEXT_PLAIN = "text/plain"
    TEXT_MARKDOWN = "text/markdown"
    TEXT_HTML = "text/html"


class _IngestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    source_app: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    source_workspace: str | None = None
    source_url: str | None = Field(default=None, max_length=SOURCE_URL_MAX)
    content_type: IngestMime


class InlineIngestRequest(_IngestBase):
    ingest_type: Literal["inline"]
    content: str = Field(min_length=1)


class FileIngestRequest(_IngestBase):
    ingest_type: Literal["file"]
    minio_site: str = Field(min_length=1)
    object_key: str = Field(min_length=1)


IngestRequest = Annotated[
    InlineIngestRequest | FileIngestRequest,
    Field(discriminator="ingest_type"),
]
