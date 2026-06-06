"""Pydantic models for POST /retrieve/v1 (spec §3.4.4)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ragent.schemas._common import FILTER_MAX_LEN, FILTER_META_MAX_LEN, validate_filter_str
from ragent.utility.env import int_env, optional_float_env

DEFAULT_TOP_K: int = int_env("RETRIEVAL_TOP_K", 20)
DEFAULT_MIN_SCORE: float | None = optional_float_env("RETRIEVAL_MIN_SCORE")


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    source_app: str | None = None
    source_meta: str | None = None
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=200)
    min_score: float | None = Field(default=DEFAULT_MIN_SCORE, ge=0.0)
    dedupe: bool = False

    @field_validator("source_app", mode="before")
    @classmethod
    def _validate_source_app(cls, v: str | None) -> str | None:
        return validate_filter_str(v, name="source_app", max_len=FILTER_MAX_LEN)

    @field_validator("source_meta", mode="before")
    @classmethod
    def _validate_source_meta(cls, v: str | None) -> str | None:
        return validate_filter_str(v, name="source_meta", max_len=FILTER_META_MAX_LEN)


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
