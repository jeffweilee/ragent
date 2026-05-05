"""POST /retrieve — standalone retrieval without LLM (spec §3.4.4)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ragent.pipelines.chat import build_es_filters, doc_to_source_entry, run_retrieval

_FILTER_MAX_LEN = 64


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    source_app: str | None = None
    source_workspace: str | None = None
    dedupe: bool = False

    @field_validator("source_app", "source_workspace", mode="before")
    @classmethod
    def _validate_filter(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v == "" or len(v) > _FILTER_MAX_LEN:
            raise ValueError(f"filter field must be 1–{_FILTER_MAX_LEN} chars")
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
        docs = await run_in_threadpool(
            run_retrieval,
            retrieval_pipeline,
            query=body.query,
            filters=build_es_filters(body.source_app, body.source_workspace),
        )
        if body.dedupe:
            docs = _dedupe_by_document(docs)
        return JSONResponse({"chunks": [doc_to_source_entry(d) for d in docs]})

    return router
