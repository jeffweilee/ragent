"""POST /retrieve — standalone retrieval without LLM (spec §3.4.4)."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, Field, field_validator

from ragent.pipelines.chat import build_es_filters, doc_to_source_entry, run_retrieval
from ragent.schemas.ingest import SOURCE_META_MAX

_FILTER_MAX_LEN = 64
_FILTER_META_MAX_LEN = SOURCE_META_MAX
logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    source_app: str | None = None
    source_meta: str | None = None
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


def _dedupe_by_document(docs: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result = []
    for doc in docs:
        doc_id = (doc.meta or {}).get("document_id")
        if doc_id not in seen:
            if doc_id is not None:
                seen.add(doc_id)
            result.append(doc)
    return result


def create_retrieve_router(retrieval_pipeline: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/retrieve")
    async def retrieve(
        body: RetrieveRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> JSONResponse:
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
                    docs = _dedupe_by_document(docs)
                    d_span.set_attribute("input_count", input_count)
                    d_span.set_attribute("output_count", len(docs))
                    logger.info(
                        "retrieve.dedupe",
                        input_count=input_count,
                        output_count=len(docs),
                    )
            return JSONResponse({"chunks": [doc_to_source_entry(d) for d in docs]})

    return router
