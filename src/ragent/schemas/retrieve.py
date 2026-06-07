"""Pydantic models for POST /retrieve/v1 (spec §3.4.4)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ragent.schemas._common import FILTER_MAX_LEN, FILTER_META_MAX_LEN, validate_filter_str
from ragent.utility.env import int_env, optional_float_env

DEFAULT_TOP_K: int = int_env("RETRIEVAL_TOP_K", 20)
if not 1 <= DEFAULT_TOP_K <= 200:
    raise RuntimeError(
        f"RETRIEVAL_TOP_K={DEFAULT_TOP_K} violates the [1, 200] top_k field constraint "
        f"(spec §3.4.4); omitted top_k in requests would bypass the API contract."
    )
DEFAULT_MIN_SCORE: float | None = optional_float_env("RETRIEVAL_MIN_SCORE")
if DEFAULT_MIN_SCORE is not None and DEFAULT_MIN_SCORE < 0.0:
    raise RuntimeError(
        f"RETRIEVAL_MIN_SCORE={DEFAULT_MIN_SCORE} must be >= 0.0 — "
        f"score thresholds cannot be negative."
    )


class RetrieveRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Natural-language question or topic to search for. "
            "Write as a full question or statement rather than keyword strings — "
            "both semantic and keyword matching are applied."
        ),
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=200,
        description=(
            f"Maximum chunks to return, ranked by relevance (1–200, default {DEFAULT_TOP_K}). "
            "Increase for broad topics needing more evidence; "
            "decrease for focused lookups. Each chunk is typically 200–800 tokens."
        ),
    )
    source_app: str | None = Field(
        default=None,
        min_length=1,
        max_length=FILTER_MAX_LEN,
        description=(
            f"Restrict results to documents from one source application "
            f"(exact match, max {FILTER_MAX_LEN} chars). "
            "Use a value from the `source_app` field in a previous retrieve result — "
            "omit on the first call to search across all sources."
        ),
    )
    source_meta: str | None = Field(
        default=None,
        min_length=1,
        max_length=FILTER_META_MAX_LEN,
        description=(
            "Restrict results to documents tagged with this exact source_meta value "
            f"(product, team, or category label; max {FILTER_META_MAX_LEN} chars). "
            "Omit to search without this filter."
        ),
    )
    min_score: float | None = Field(
        default=DEFAULT_MIN_SCORE,
        ge=0.0,
        description=(
            "Exclude chunks below this relevance score (≥ 0.0). "
            "Use 0.7 for high-confidence results only. "
            "Omit to return all top_k results regardless of score — "
            "recommended for exploratory queries."
        ),
    )
    dedupe: bool = Field(
        default=False,
        description=(
            "When true, return at most one chunk per source document (highest-scored). "
            "Set true for broad topic coverage across different documents; "
            "leave false to allow multiple excerpts from the same document."
        ),
    )

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
