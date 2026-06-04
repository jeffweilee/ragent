"""Pydantic schemas for retrieve request and response payloads."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ragent.schemas.ingest import SOURCE_META_MAX
from ragent.utility.retrieval_defaults import DEFAULT_MIN_SCORE, DEFAULT_TOP_K

SOURCE_APP_MAX = 64


class RetrieveRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Natural-language search query used to retrieve relevant document chunks "
            "from the ragent corpus."
        ),
    )
    source_app: str | None = Field(
        default=None,
        min_length=1,
        max_length=SOURCE_APP_MAX,
        description=(
            "Optional exact-match filter for the source application, such as an ingest "
            "connector or upstream system name."
        ),
        json_schema_extra={"x-mcp-enum-env": "RAGENT_MCP_RETRIEVE_SOURCE_APP_ALLOWLIST"},
    )
    source_meta: str | None = Field(
        default=None,
        min_length=1,
        max_length=SOURCE_META_MAX,
        description=(
            "Optional exact-match filter for source metadata, such as a workspace, "
            "space, channel, team, or project."
        ),
        json_schema_extra={"x-mcp-exposed": False},
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=200,
        description=(
            "Maximum number of ranked chunks to return. Use smaller values for concise "
            "answers and larger values when broader evidence is needed."
        ),
    )
    min_score: float | None = Field(
        default=DEFAULT_MIN_SCORE,
        ge=0.0,
        description=(
            "Optional retrieval relevance score floor. Chunks below this threshold are "
            "removed after retrieval and reranking."
        ),
        json_schema_extra={"x-mcp-exposed": False},
    )
    dedupe: bool = Field(
        default=False,
        description=(
            "When true, return at most one chunk per document_id, preserving the "
            "highest-ranked chunk from each document."
        ),
    )


class ChunkEntry(BaseModel):
    document_id: str | None
    source_app: str | None
    source_id: str | None
    source_meta: str | None
    type: str
    source_title: str | None
    source_url: str | None
    mime_type: str | None
    excerpt: str
    score: float | None


class RetrieveResponse(BaseModel):
    chunks: list[ChunkEntry]
