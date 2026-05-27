"""T3.10 — POST /chat (non-streaming) and T3.12 — POST /chat/stream (SSE) (B12, S6a-S6e)."""

from __future__ import annotations

import json
import math
import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace

from ragent.auth.deps import get_user_id
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.errors.upstream import LLMStreamInterruptedError
from ragent.pipelines.retrieve import (
    EXCERPT_MAX_CHARS_DEFAULT,
    build_es_filters,
    dedupe_by_document,
    doc_to_source_entry,
    run_retrieval,
)
from ragent.schemas.chat import ChatRequest, build_rag_messages, normalize_citations
from ragent.utility.feedback_token import compute_sources_hash
from ragent.utility.feedback_token import sign as sign_feedback_token
from ragent.utility.id_gen import new_id

# ---------------------------------------------------------------------------
# Intent taxonomy — 維護點：只改此表即可新增/移除 intent
# ---------------------------------------------------------------------------
_INTENT_REQUIRES_RETRIEVE: dict[str, bool] = {
    "GREETING": False,  # 打招呼、再見
    "CHITCHAT": False,  # 閒聊、情緒表達、開放式創作（詩/故事）
    "QUESTION": True,  # 需從文件查找事實
    "SUMMARY": True,  # 摘要文件內容
    "GENERATION": True,  # 根據文件草擬文字（必須依賴文件；純創作 → CHITCHAT）
}
_INTENT_DEFAULT = "QUESTION"  # fallback when LLM returns unrecognised label

# Per-intent temperature used when body.temperature is None.
_DEFAULT_TEMPERATURE = 0.7
_INTENT_TEMPERATURE: dict[str, float] = {
    "GREETING": 0.8,  # warm, natural conversation
    "CHITCHAT": 0.8,  # creative, varied
    "QUESTION": 0.2,  # factual, low variance
    "SUMMARY": 0.2,  # faithful to source
    "GENERATION": 0.7,  # fluent yet grounded
}

_INTENT_SYSTEM_PROMPT = (
    "Classify the user message into exactly one intent label:\n"
    "  GREETING   — greetings, farewells, pleasantries\n"
    "  CHITCHAT   — casual conversation, emotional expression, small talk, "
    "open-ended creative requests (poems, stories, jokes) NOT tied to documents\n"
    "  QUESTION   — factual question to be answered from documents\n"
    "  SUMMARY    — request to summarise document content\n"
    "  GENERATION — request to draft or write content that MUST be grounded in "
    "documents (e.g. 'write a reply based on this contract'); "
    "pure creative writing with no document dependency → CHITCHAT\n"
    "Reply with only the intent label. No punctuation, no explanation."
)


logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


def _requires_retrieve(intent: str) -> bool:
    """Return True when the given intent requires retrieval.

    Unknown intents default to True (fail-safe: better to over-retrieve
    than to silently skip useful context).
    """
    return _INTENT_REQUIRES_RETRIEVE.get(intent, True)


def _compute_skip_retrieve(context_mode: str, intent: str) -> bool:
    """Determine whether retrieval should be skipped.

    context_mode='force'  → always retrieve (skip=False)
    context_mode='caller' → always skip (skip=True); caller embeds own <context>
    context_mode='auto'   → delegate to intent taxonomy
    """
    if context_mode == "force":
        return False
    if context_mode == "caller":
        return True
    # auto: delegate to intent
    return not _requires_retrieve(intent)


def _resolve_temperature(body_temperature: float | None, intent: str) -> float:
    """Return the effective LLM temperature.

    Caller-supplied temperature takes precedence; None triggers intent-based auto-selection
    from _INTENT_TEMPERATURE. Unknown intents fall back to _DEFAULT_TEMPERATURE.
    """
    return (
        body_temperature
        if body_temperature is not None
        else _INTENT_TEMPERATURE.get(intent, _DEFAULT_TEMPERATURE)
    )


def _detect_intent(llm_client: Any, query: str, model: str) -> str:
    """Call LLM to classify the user query intent.

    Uses temperature=0 and max_tokens=10 for a fast, deterministic
    single-word response. The first word of the response is used so
    the function is robust to LLM explanations appended after the label.
    Returns _INTENT_DEFAULT on any error or unrecognised label (fail-safe).
    """
    messages = [
        {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]
    try:
        result = llm_client.chat(messages=messages, model=model, temperature=0, max_tokens=10)
        raw = ((result or {}).get("content") or "").strip().upper()
        words = raw.split()
        # Strip non-alpha chars (e.g. trailing punctuation "GREETING.") before lookup.
        label = "".join(c for c in (words[0] if words else "") if c.isalpha())
        return label if label in _INTENT_REQUIRES_RETRIEVE else _INTENT_DEFAULT
    except Exception:
        logger.warning("chat.intent.error", exc_info=True)
        return _INTENT_DEFAULT  # fail-safe: assume QUESTION → retrieval still runs


async def _resolve_docs(
    body: ChatRequest,
    last_user: str,
    llm_client: Any,
    retrieval_pipeline: Any,
) -> tuple[list[Any], bool, str]:
    """Detect intent (always) and conditionally run retrieval.

    Returns (docs, skip_retrieve, intent).

    Intent detection always runs regardless of context_mode — it drives both
    prompt selection and temperature. Retrieval is then gated by
    _compute_skip_retrieve(context_mode, intent).
    """
    intent = _INTENT_DEFAULT
    if last_user.strip():
        with _tracer.start_as_current_span("chat.intent") as i_span:
            intent = await run_in_threadpool(_detect_intent, llm_client, last_user, body.model)
            i_span.set_attribute("intent", intent)
            logger.info("chat.intent", intent=intent, query_len=len(last_user))

    skip_retrieve = _compute_skip_retrieve(body.context_mode, intent)
    docs: list[Any] = []

    if not skip_retrieve:
        with _tracer.start_as_current_span("chat.retrieval") as r_span:
            r_span.set_attribute("query_len", len(last_user))
            docs = await run_in_threadpool(_run_retrieval, retrieval_pipeline, body, last_user)
            if body.dedupe:
                docs = dedupe_by_document(docs)
            r_span.set_attribute("result_count", len(docs))
            logger.info("chat.retrieval", query_len=len(last_user), result_count=len(docs))
    else:
        logger.info(
            "chat.retrieval.skipped",
            context_mode=body.context_mode,
            intent=intent if body.context_mode == "auto" else None,
            query_len=len(last_user),
        )

    return docs, skip_retrieve, intent


def _extract_token_counts(usage: dict) -> tuple[int | None, int | None]:
    # LLMClient.chat returns camelCase keys; streaming uses snake_case — tolerate both.
    prompt = usage.get("promptTokens", usage.get("prompt_tokens"))
    completion = usage.get("completionTokens", usage.get("completion_tokens"))
    return (
        int(prompt) if prompt is not None else None,
        int(completion) if completion is not None else None,
    )


def _build_sources(documents: list[Any], max_chars: int) -> list[dict]:
    """Build source entries from retrieved documents.

    Returns [] when documents is empty — distinct from None (retrieval skipped).
    sources semantics: None=skipped, []=ran+no hits, [{...}]=ran+found
    """
    return [doc_to_source_entry(d, max_chars=max_chars) for d in documents]


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


def _run_retrieval(retrieval_pipeline: Any, req: ChatRequest, last_user: str) -> list[Any]:
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
    excerpt_max_chars: int = EXCERPT_MAX_CHARS_DEFAULT,
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
        x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
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
            docs, skip_retrieve, intent = await _resolve_docs(
                body, last_user, llm_client, retrieval_pipeline
            )
            with _tracer.start_as_current_span("chat.build_messages"):
                messages = build_rag_messages(
                    body, docs, inject_context=not skip_retrieve, intent=intent
                )
            effective_temperature = _resolve_temperature(body.temperature, intent)
            with _tracer.start_as_current_span("chat.llm") as l_span:
                l_span.set_attribute("model", body.model)
                result = await run_in_threadpool(
                    llm_client.chat,
                    messages=messages,
                    model=body.model,
                    temperature=effective_temperature,
                    max_tokens=body.max_tokens,
                )
                usage = result.get("usage") or {}
                prompt_tokens, completion_tokens = _extract_token_counts(usage)
                if prompt_tokens is not None:
                    l_span.set_attribute("prompt_tokens", prompt_tokens)
                if completion_tokens is not None:
                    l_span.set_attribute("completion_tokens", completion_tokens)
                logger.info(
                    "chat.llm",
                    model=body.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            # sources semantics: None=skipped, []=ran+no hits, [{...}]=ran+found
            sources = None if skip_retrieve else _build_sources(docs, max_chars=excerpt_max_chars)
            content = normalize_citations(result.get("content") or "")
            return JSONResponse(
                {
                    "content": content,
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
        x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
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
            docs, skip_retrieve, intent = await _resolve_docs(
                body, last_user, llm_client, retrieval_pipeline
            )
            with _tracer.start_as_current_span("chat.build_messages"):
                messages = build_rag_messages(
                    body, docs, inject_context=not skip_retrieve, intent=intent
                )
            effective_temperature = _resolve_temperature(body.temperature, intent)
            # sources semantics: None=skipped, []=ran+no hits, [{...}]=ran+found
            sources = None if skip_retrieve else _build_sources(docs, max_chars=excerpt_max_chars)
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
                    usage_out: list = []
                    for delta in llm_client.stream(
                        messages=messages,
                        model=body.model,
                        temperature=effective_temperature,
                        max_tokens=body.max_tokens,
                        usage_out=usage_out,
                    ):
                        # Best-effort per-delta normalization: catches full-width brackets
                        # emitted as a single token (the common case). Brackets split across
                        # chunk boundaries will not be caught here but ARE normalized in
                        # the assembled done.content below.
                        normalized_delta = normalize_citations(delta)
                        full_content.append(normalized_delta)
                        delta_payload = {"type": "delta", "content": normalized_delta}
                        yield f"data: {json.dumps(delta_payload)}\n\n"
                    assembled = normalize_citations("".join(full_content))
                    done_payload = {
                        "type": "done",
                        "content": assembled,
                        "model": body.model,
                        "provider": body.provider,
                        "sources": sources,
                        **_maybe_mint_feedback_envelope(feedback_hmac_secret, x_user_id, sources),
                    }
                    yield f"data: {json.dumps(done_payload)}\n\n"
                    prompt_tokens, completion_tokens = _extract_token_counts(
                        usage_out[0] if usage_out else {}
                    )
                    if prompt_tokens is not None:
                        l_span.set_attribute("prompt_tokens", prompt_tokens)
                    if completion_tokens is not None:
                        l_span.set_attribute("completion_tokens", completion_tokens)
                    logger.info(
                        "chat.llm",
                        model=body.model,
                        completion_chars=len(assembled),
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                except Exception as exc:
                    l_span.record_exception(exc)
                    if isinstance(exc, LLMStreamInterruptedError):
                        logger.warning(
                            "chat.llm.stream_interrupted",
                            model=body.model,
                            error_type=type(exc).__name__,
                        )
                    else:
                        logger.error(
                            "chat.llm.error",
                            model=body.model,
                            error_type=type(exc).__name__,
                        )
                    error_code = getattr(exc, "error_code", HttpErrorCode.LLM_ERROR)
                    err_payload = {
                        "type": "error",
                        "error_code": str(error_code),
                        "message": str(exc),
                    }
                    yield f"data: {json.dumps(err_payload)}\n\n"

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router
