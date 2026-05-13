"""POST /retrieve — standalone retrieval without LLM (spec §3.4.4)."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from opentelemetry import trace
from pydantic import BaseModel, Field, field_validator

from ragent.pipelines.chat import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    build_es_filters,
    dedupe_by_document,
    doc_to_source_entry,
    run_retrieval,
)
from ragent.schemas.ingest import SOURCE_META_MAX

_FILTER_MAX_LEN = 64
_FILTER_META_MAX_LEN = SOURCE_META_MAX
logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


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
        if v is None:
            return v
        if v == "" or len(v) > _FILTER_MAX_LEN:
            raise ValueError(f"source_app must be 1–{_FILTER_MAX_LEN} chars")
        return v

    @field_validator("source_meta", mode="before")
    @classmethod
    def _validate_source_meta(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v == "" or len(v) > _FILTER_META_MAX_LEN:
            raise ValueError(f"source_meta must be 1–{_FILTER_META_MAX_LEN} chars")
        return v


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


def create_retrieve_router(retrieval_pipeline: Any) -> APIRouter:
    router = APIRouter(prefix="/retrieve/v1")

    @router.post("", response_model=RetrieveResponse)
    async def retrieve(
        body: RetrieveRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> RetrieveResponse:
        with _tracer.start_as_current_span("retrieve.request") as span:
            if x_user_id:
                span.set_attribute("user_id", x_user_id)
            span.set_attribute("query_len", len(body.query))
            span.set_attribute("dedupe", body.dedupe)
            with _tracer.start_as_current_span("retrieve.pipeline") as p_span:
                docs = await run_in_threadpool(
                    run_retrieval,
                    retrieval_pipeline,
                    query=body.query,
                    filters=build_es_filters(body.source_app, body.source_meta),
                    top_k=body.top_k,
                    min_score=body.min_score,
                )
                p_span.set_attribute("result_count", len(docs))
                logger.info(
                    "retrieve.pipeline",
                    query_len=len(body.query),
                    result_count=len(docs),
                )
            if body.dedupe:
                input_count = len(docs)
                with _tracer.start_as_current_span("retrieve.dedupe") as d_span:
                    docs = dedupe_by_document(docs)
                    d_span.set_attribute("input_count", input_count)
                    d_span.set_attribute("output_count", len(docs))
                    logger.info(
                        "retrieve.dedupe",
                        input_count=input_count,
                        output_count=len(docs),
                    )
            return RetrieveResponse(chunks=[ChunkEntry(**doc_to_source_entry(d)) for d in docs])

    return router
