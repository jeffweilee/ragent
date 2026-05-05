"""T3.10 — POST /chat (non-streaming) and T3.12 — POST /chat/stream (SSE) (B12, S6a-S6e)."""

from __future__ import annotations

import json
import math
import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.problem import problem
from ragent.pipelines.chat import EXCERPT_MAX_CHARS, run_retrieval
from ragent.schemas.chat import ChatRequest, build_rag_messages


def _build_sources(documents: list[Any]) -> list[dict] | None:
    if not documents:
        return None
    sources = []
    for doc in documents:
        meta = doc.meta or {}
        sources.append(
            {
                "type": "knowledge",
                "document_id": meta.get("document_id"),
                "source_app": meta.get("source_app"),
                "source_id": meta.get("source_id"),
                "source_title": meta.get("source_title"),
                "excerpt": (doc.content or "")[:EXCERPT_MAX_CHARS],
            }
        )
    return sources


def _run_retrieval(retrieval_pipeline: Any, req: ChatRequest) -> list[Any]:
    last_user = next((m["content"] for m in reversed(req.messages) if m.get("role") == "user"), "")
    clauses = []
    if req.source_app:
        clauses.append({"field": "source_app", "operator": "==", "value": req.source_app})
    if req.source_workspace:
        clauses.append(
            {"field": "source_workspace", "operator": "==", "value": req.source_workspace}
        )
    if not clauses:
        filters = None
    elif len(clauses) == 1:
        filters = clauses[0]
    else:
        filters = {"operator": "AND", "conditions": clauses}
    return run_retrieval(retrieval_pipeline, query=last_user, filters=filters)


def _rate_limit_response(reset_at: float) -> Response:
    retry_after = max(1, math.ceil(reset_at - time.time()))
    resp = problem(429, "CHAT_RATE_LIMITED", "Too Many Requests")
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def create_chat_router(
    retrieval_pipeline: Any,
    llm_client: Any,
    rate_limiter: RateLimiter | None = None,
    rate_limit: int = 60,
    rate_limit_window: int = 60,
) -> APIRouter:
    router = APIRouter()

    def _check_rate(user_id: str | None) -> Response | None:
        if rate_limiter is None or user_id is None:
            return None
        result = rate_limiter.check(
            f"chat:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        if not result.allowed:
            return _rate_limit_response(result.reset_at or 0)
        return None

    @router.post("/chat")
    async def chat(
        body: ChatRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> Response:
        if (blocked := _check_rate(x_user_id)) is not None:
            return blocked
        docs = await run_in_threadpool(_run_retrieval, retrieval_pipeline, body)
        messages = build_rag_messages(body, docs)
        result = await run_in_threadpool(
            llm_client.chat,
            messages=messages,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
        return JSONResponse(
            {
                "content": result["content"],
                "usage": result["usage"],
                "model": body.model,
                "provider": body.provider,
                "sources": _build_sources(docs),
            }
        )

    @router.post("/chat/stream")
    async def chat_stream(
        body: ChatRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> Response:
        if (blocked := _check_rate(x_user_id)) is not None:
            return blocked
        docs = await run_in_threadpool(_run_retrieval, retrieval_pipeline, body)
        messages = build_rag_messages(body, docs)
        sources = _build_sources(docs)

        def _generate():
            try:
                full_content = []
                for delta in llm_client.stream(
                    messages=messages,
                    model=body.model,
                    temperature=body.temperature,
                    max_tokens=body.max_tokens,
                ):
                    full_content.append(delta)
                    yield f"data: {json.dumps({'type': 'delta', 'content': delta})}\n\n"
                done_payload = {
                    "type": "done",
                    "content": "".join(full_content),
                    "model": body.model,
                    "provider": body.provider,
                    "sources": sources,
                }
                yield f"data: {json.dumps(done_payload)}\n\n"
            except Exception as exc:
                err_payload = {"type": "error", "error_code": "LLM_ERROR", "message": str(exc)}
                yield f"data: {json.dumps(err_payload)}\n\n"

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router
