"""POST /retrieve — standalone retrieval without LLM (spec §3.4, B12)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ragent.pipelines.chat import EXCERPT_MAX_CHARS, run_retrieval

_FILTER_MAX_LEN = 64


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    source_app: str | None = None
    source_workspace: str | None = None
    dedupe: bool = False


def _build_filters(req: RetrieveRequest) -> dict | None:
    clauses = []
    if req.source_app:
        clauses.append({"field": "source_app", "operator": "==", "value": req.source_app})
    if req.source_workspace:
        clauses.append(
            {"field": "source_workspace", "operator": "==", "value": req.source_workspace}
        )
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"operator": "AND", "conditions": clauses}


def _to_chunk(doc: Any) -> dict:
    meta = doc.meta or {}
    return {
        "document_id": meta.get("document_id"),
        "source_app": meta.get("source_app"),
        "source_id": meta.get("source_id"),
        "type": "knowledge",
        "source_title": meta.get("source_title"),
        "excerpt": (doc.content or "")[:EXCERPT_MAX_CHARS],
    }


def _dedupe_by_document(docs: list[Any]) -> list[Any]:
    """Keep the first (highest-scored) chunk per document_id. Order preserved."""
    seen: set[str] = set()
    result = []
    for doc in docs:
        doc_id = (doc.meta or {}).get("document_id")
        if doc_id is None or doc_id not in seen:
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
        filters = _build_filters(body)
        docs = await run_in_threadpool(
            run_retrieval, retrieval_pipeline, query=body.query, filters=filters
        )
        if body.dedupe:
            docs = _dedupe_by_document(docs)
        return JSONResponse({"chunks": [_to_chunk(d) for d in docs]})

    return router
