"""T3.10 — POST /chat (non-streaming) and T3.12 — POST /chat/stream (SSE) (B12, S6a-S6e)."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse

from ragent.schemas.chat import ChatRequest, normalize_messages

_PIPELINE_INPUT_KEY = "query_embedder"
_PIPELINE_OUTPUT_KEY = "source_hydrator"


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
                "excerpt": doc.content,
            }
        )
    return sources


def _run_retrieval(retrieval_pipeline: Any, req: ChatRequest) -> list[Any]:
    last_user = next(
        (m["content"] for m in reversed(req.messages) if m.get("role") == "user"), ""
    )
    result = retrieval_pipeline.run({_PIPELINE_INPUT_KEY: {"query": last_user}})
    return result.get(_PIPELINE_OUTPUT_KEY, {}).get("documents", [])


def create_chat_router(retrieval_pipeline: Any, llm_client: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/chat")
    async def chat(
        body: ChatRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> JSONResponse:
        messages = normalize_messages(body)
        docs = _run_retrieval(retrieval_pipeline, body)
        result = llm_client.chat(
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
    ) -> StreamingResponse:
        messages = normalize_messages(body)
        docs = _run_retrieval(retrieval_pipeline, body)
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
