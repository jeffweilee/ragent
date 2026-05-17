"""T3.10 — POST /chat (non-streaming) and T3.12 — POST /chat/stream (SSE) (B12, S6a-S6e)."""

from __future__ import annotations

import json
import math
import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace

from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.pipelines.chat import build_es_filters, doc_to_source_entry, run_retrieval
from ragent.schemas.chat import ChatRequest, build_rag_messages
from ragent.utility.feedback_token import compute_sources_hash
from ragent.utility.feedback_token import sign as sign_feedback_token
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


def _build_sources(documents: list[Any]) -> list[dict] | None:
    if not documents:
        return None
    return [doc_to_source_entry(d) for d in documents]


def _maybe_mint_feedback_envelope(
    hmac_secret: str | None,
    user_id: str | None,
    sources: list[dict] | None,
) -> dict:
    """Return {request_id, feedback_token} when HMAC is configured, else {} (B55).

    The token binds ``(source_app, source_id)`` **pairs** (B11/B35 identity)
    so feedback against a forged ``source_app`` for a known ``source_id`` is
    rejected at verify time.
    """
    if not hmac_secret or not user_id:
        return {}
    request_id = new_id()
    source_refs = [
        (s["source_app"], s["source_id"])
        for s in (sources or [])
        if s.get("source_id") and s.get("source_app")
    ]
    token = sign_feedback_token(
        {
            "request_id": request_id,
            "user_id": user_id,
            "sources_hash": compute_sources_hash(source_refs),
            "ts": int(time.time()),
        },
        hmac_secret,
    )
    return {"request_id": request_id, "feedback_token": token}


def _run_retrieval(retrieval_pipeline: Any, req: ChatRequest) -> list[Any]:
    last_user = next((m["content"] for m in reversed(req.messages) if m.get("role") == "user"), "")
    return run_retrieval(
        retrieval_pipeline,
        query=last_user,
        filters=build_es_filters(req.source_app, req.source_meta),
        top_k=req.top_k,
        min_score=req.min_score,
    )


def _rate_limit_response(reset_at: float) -> Response:
    retry_after = max(1, math.ceil(reset_at - time.time()))
    resp = problem(429, HttpErrorCode.CHAT_RATE_LIMITED, "Too Many Requests")
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def create_chat_router(
    retrieval_pipeline: Any,
    llm_client: Any,
    rate_limiter: RateLimiter | None = None,
    rate_limit: int = 60,
    rate_limit_window: int = 60,
    feedback_hmac_secret: str | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/chat/v1")

    def _check_rate(user_id: str | None) -> Response | None:
        if rate_limiter is None or user_id is None:
            return None
        result = rate_limiter.check(
            f"chat:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        if not result.allowed:
            logger.warning(
                "chat.rate_limited",
                user_id=user_id,
                limit=rate_limit,
                window_seconds=rate_limit_window,
                error_code=HttpErrorCode.CHAT_RATE_LIMITED,
                http_status=429,
            )
            return _rate_limit_response(result.reset_at or 0)
        return None

    @router.post("")
    async def chat(
        body: ChatRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> Response:
        with _tracer.start_as_current_span("chat.request") as span:
            span.set_attribute("model", body.model)
            span.set_attribute("provider", body.provider)
            span.set_attribute("stream", False)
            if x_user_id:
                span.set_attribute("user_id", x_user_id)
            if (blocked := _check_rate(x_user_id)) is not None:
                return blocked
            last_user = next(
                (m["content"] for m in reversed(body.messages) if m.get("role") == "user"),
                "",
            )
            with _tracer.start_as_current_span("chat.retrieval") as r_span:
                r_span.set_attribute("query_len", len(last_user))
                docs = await run_in_threadpool(_run_retrieval, retrieval_pipeline, body)
                r_span.set_attribute("result_count", len(docs))
                logger.info(
                    "chat.retrieval",
                    query_len=len(last_user),
                    result_count=len(docs),
                )
            with _tracer.start_as_current_span("chat.build_messages"):
                messages = build_rag_messages(body, docs)
            with _tracer.start_as_current_span("chat.llm") as l_span:
                l_span.set_attribute("model", body.model)
                result = await run_in_threadpool(
                    llm_client.chat,
                    messages=messages,
                    model=body.model,
                    temperature=body.temperature,
                    max_tokens=body.max_tokens,
                )
                usage = result.get("usage") or {}
                # LLMClient.chat returns camelCase usage keys; tolerate snake_case from
                # alternative providers as well.
                prompt_tokens = usage.get("promptTokens", usage.get("prompt_tokens"))
                completion_tokens = usage.get("completionTokens", usage.get("completion_tokens"))
                if prompt_tokens is not None:
                    l_span.set_attribute("prompt_tokens", int(prompt_tokens))
                if completion_tokens is not None:
                    l_span.set_attribute("completion_tokens", int(completion_tokens))
                logger.info(
                    "chat.llm",
                    model=body.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            sources = _build_sources(docs)
            return JSONResponse(
                {
                    "content": result["content"],
                    "usage": result["usage"],
                    "model": body.model,
                    "provider": body.provider,
                    "sources": sources,
                    **_maybe_mint_feedback_envelope(feedback_hmac_secret, x_user_id, sources),
                }
            )

    @router.post("/stream")
    async def chat_stream(
        body: ChatRequest,
        x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    ) -> Response:
        with _tracer.start_as_current_span("chat.request") as request_span:
            request_span.set_attribute("model", body.model)
            request_span.set_attribute("provider", body.provider)
            request_span.set_attribute("stream", True)
            if x_user_id:
                request_span.set_attribute("user_id", x_user_id)
            if (blocked := _check_rate(x_user_id)) is not None:
                return blocked
            last_user = next(
                (m["content"] for m in reversed(body.messages) if m.get("role") == "user"),
                "",
            )
            with _tracer.start_as_current_span("chat.retrieval") as r_span:
                r_span.set_attribute("query_len", len(last_user))
                docs = await run_in_threadpool(_run_retrieval, retrieval_pipeline, body)
                r_span.set_attribute("result_count", len(docs))
                logger.info(
                    "chat.retrieval",
                    query_len=len(last_user),
                    result_count=len(docs),
                )
            with _tracer.start_as_current_span("chat.build_messages"):
                messages = build_rag_messages(body, docs)
            sources = _build_sources(docs)
            # Capture the chat.request context so chat.llm (started later inside the
            # StreamingResponse generator, in a different async context) still nests
            # under chat.request via explicit parent reference rather than via the
            # OTEL contextvars stack — which would raise "Failed to detach context".
            parent_ctx = trace.set_span_in_context(request_span)

        def _generate():
            with _tracer.start_as_current_span("chat.llm", context=parent_ctx) as l_span:
                l_span.set_attribute("model", body.model)
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
                        **_maybe_mint_feedback_envelope(feedback_hmac_secret, x_user_id, sources),
                    }
                    yield f"data: {json.dumps(done_payload)}\n\n"
                    logger.info(
                        "chat.llm",
                        model=body.model,
                        completion_chars=sum(len(c) for c in full_content),
                    )
                except Exception as exc:
                    l_span.record_exception(exc)
                    logger.exception(
                        "chat.llm.error",
                        model=body.model,
                        error_type=type(exc).__name__,
                    )
                    err_payload = {
                        "type": "error",
                        "error_code": "LLM_ERROR",
                        "message": str(exc),
                    }
                    yield f"data: {json.dumps(err_payload)}\n\n"

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router
