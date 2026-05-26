"""T3.4 — ChatRequest schema with env defaults and filter validation (B12, B21, B22, B29)."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ragent.pipelines.retrieve import DEFAULT_MIN_SCORE as _DEFAULT_MIN_SCORE
from ragent.pipelines.retrieve import DEFAULT_TOP_K as _DEFAULT_TOP_K
from ragent.schemas.ingest import SOURCE_META_MAX

_DEFAULT_PROVIDER = os.environ.get("RAGENT_DEFAULT_LLM_PROVIDER", "openai")
_DEFAULT_MODEL = os.environ.get("RAGENT_DEFAULT_LLM_MODEL", "gptoss-120b")
_DEFAULT_TEMPERATURE = float(os.environ.get("RAGENT_DEFAULT_TEMPERATURE", "0.7"))
_DEFAULT_MAX_TOKENS = int(os.environ.get("RAGENT_DEFAULT_MAX_TOKENS", "4096"))
_MARKDOWN_OUTPUT_RULE = (
    "Always format your response in Markdown (headings, lists, code fences, "
    "emphasis as appropriate). Output Markdown only — no surrounding code "
    "fence wrapping the entire reply."
)
# =====================================================================
# Shared RAG grounding rules — injected into both system prompt variants.
# IMPORTANT: any literal { } in this string that must NOT be treated as
# f-string placeholders must be doubled: {{ }}.
# =====================================================================
_RAG_COMMON_INSTRUCTIONS = """\
[CRITICAL GROUNDING RULES]
1. CHITCHAT & GREETINGS: If the user turn is conceptually a greeting, conversational
   pleasantry, short casual acknowledgement, or emotional expression (including slang
   like "嗨嗨", "安安", "Hi there", "Thanks!"), reply warmly and naturally. Mirror the
   user's language and script perfectly. Do NOT apply context restrictions for these.
2. FACTUAL QUESTIONS: Use ONLY facts directly mentioned inside <context>...</context>.
   Do not rely on prior knowledge or external assumptions.
3. CITATION RULE (GEMINI STYLE): Cite using ONLY the exact numeric format `[1]`, `[2]`
   based on the corresponding [資料來源 #X] index. Place citations at the paragraph end.
   CRITICAL BAN: NEVER use `【1】`, `(1)`, `[#1]`, `1.` or document titles as citations.
4. NATURAL REFUSAL: If context lacks the answer to a factual question, reply
   empathetically in the user's language and script:
   - Traditional Chinese → "我理解您的問題，但從目前提供的資料中找不到相關資訊。"
   - Simplified Chinese  → "我理解您的问题，但从目前提供的资料中找不到相关信息。"
   - English             → "I understand your question, but the provided context does not
                           contain the relevant information."
   - Japanese            → "ご質問の趣旨は理解いたしました。しかしながら、提供された資料の中には
                           関連する情報が見つかりません。"
   NEVER use robotic phrases like "I don't know based on the provided context."
5. STRUCTURE GUARD: NEVER repeat or echo delimiter tokens like `<context>` or
   `</context>` in your response.
6. STRICT LANGUAGE MIRRORING: Mirror the script/language of the user's prompt exactly.
   If the user writes in Traditional Chinese, respond entirely in Traditional Chinese —
   even if the text inside <context> is in Simplified Chinese or English.
7. GROUNDED RESPONSE OPENER: For QUESTION, SUMMARY, and GENERATION responses, always
   begin your answer with a natural opener that grounds the reply in the retrieved
   material. Mirror the user's language/script for this opener. Examples:
   - Traditional/Simplified Chinese → "根據所提供的資料，…" / "依據相關資料，…"
   - English → "Based on the provided materials, …"
   - Japanese → "提供された資料によると、…"
"""

_DEFAULT_RAG_SYSTEM_PROMPT = os.environ.get("RAGENT_DEFAULT_RAG_SYSTEM_PROMPT") or (
    f"""\
You are a helpful, intelligent, and retrieval-grounded assistant.

{_RAG_COMMON_INSTRUCTIONS}
---
Detect the user's intent and respond in the matching style
(always mirror the user's language/script):
1. GREETING / CHITCHAT — Warm, friendly, concise. No context restriction applies.
2. QUESTION  — Answerable from context. Direct, 1–4 sentences. One `[N]` citation at paragraph end.
   Example (Traditional Chinese prompt):
   User: "Acme 什麼時候推出 v2?"
   Context: <context>[資料來源 #1] "...Acme v2 已经在 2024-03-12 正式发布..."</context>
   Assistant: "Acme v2 已於 2024-03-12 正式發佈。[1]"
3. SUMMARY   — 3–6 bullet points. Begin with the grounded opener (Rule 7).
4. GENERATION— Draft text grounded in context. Do not invent facts.

"""
    + _MARKDOWN_OUTPUT_RULE
)

_RAG_GROUNDING_RULES = os.environ.get("RAGENT_RAG_GROUNDING_RULES") or (
    f"""\
You are a retrieval-grounded assistant under strict constraints.

{_RAG_COMMON_INSTRUCTIONS}
Detect the user's intent (GREETING / QUESTION / SUMMARY / GENERATION) and apply the
matching response style. Default to QUESTION style when intent is unclear.

"""
    + _MARKDOWN_OUTPUT_RULE
)

_PROVIDER_ALLOWLIST = frozenset({"openai"})
_FILTER_MAX_LEN = 64
_FILTER_META_MAX_LEN = SOURCE_META_MAX


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    provider: str = _DEFAULT_PROVIDER
    model: str = _DEFAULT_MODEL
    temperature: float = _DEFAULT_TEMPERATURE
    max_tokens: int = _DEFAULT_MAX_TOKENS
    source_app: str | None = None
    source_meta: str | None = None
    top_k: int = Field(default=_DEFAULT_TOP_K, ge=1, le=200)
    min_score: float | None = Field(default=_DEFAULT_MIN_SCORE, ge=0.0)
    dedupe: bool = False
    retrieve: bool = True  # False = skip intent detection + retrieval; caller supplies <context>

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in _PROVIDER_ALLOWLIST:
            raise ValueError(f"provider must be one of {sorted(_PROVIDER_ALLOWLIST)}")
        return v

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


def _render_context(docs: list[Any] | None) -> str:
    """Render retrieved docs as a clean numbered list for the LLM.

    Metadata (source_app, document_id, etc.) is intentionally hidden from the model
    to prevent format confusion; only the numeric index and body are emitted.
    Returns a sentinel string when the context is empty so the RAG boundary is
    always enforced — never silently removed.
    """
    if not docs:
        return "(The context is empty.)"
    parts = []
    for i, doc in enumerate(docs, start=1):
        meta = getattr(doc, "meta", None) or {}
        # Prefer the original byte-faithful slice for LLM display; fall back
        # to normalized content when chunks predate the raw_content field.
        body = meta.get("raw_content") or (getattr(doc, "content", "") or "")
        # Escape delimiter tokens so corpus text can never close the <context> wrapper
        # early or inject a nested block (HTML/XML/code docs commonly contain these).
        body = body.replace("<context>", "&lt;context&gt;").replace(
            "</context>", "&lt;/context&gt;"
        )
        parts.append(f"[資料來源 #{i}]\n{body}\n---")
    return "\n".join(parts)


def _wrap_last_user(messages: list[dict[str, Any]], context_block: str) -> list[dict[str, Any]]:
    """Inject the context block into the last user message using XML-style tags.

    XML tags (<context>/</context>) have higher LLM recognition than plain
    ASCII fence markers and make it unambiguous where retrieved context ends.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            original = messages[i].get("content", "") or ""
            messages[i] = {
                **messages[i],
                "content": f"<context>\n{context_block}\n</context>\n\n{original}",
            }
            return messages
    return messages


def build_rag_messages(
    req: ChatRequest,
    docs: list[Any] | None,
    *,
    inject_context: bool = True,
) -> list[dict[str, Any]]:
    """Build the final message list for the LLM with RAG grounding always applied.

    The RAG system prompt (or grounding rules) is prepended in every case — even when
    docs is empty — so the boundary is never silently removed. An empty docs list
    produces a sentinel context block rather than falling back to the generic assistant.

    When inject_context=False the caller's message list is passed through verbatim
    (no <context> wrapping); use this when the caller has already embedded their own
    <context>…</context> block and retrieval was intentionally skipped.
    """
    messages = list(req.messages)
    if inject_context:
        context_block = _render_context(docs)
        messages = _wrap_last_user(messages, context_block)
    # Single pass: detect user-supplied system message and partition in one loop.
    # Float system messages to the front — some providers (e.g. OpenAI) reject
    # non-leading system messages.
    sys_msgs: list[dict[str, Any]] = []
    other_msgs: list[dict[str, Any]] = []
    for m in messages:
        (sys_msgs if m.get("role") == "system" else other_msgs).append(m)
    if sys_msgs:
        # Order: [grounding_rules, caller_system…, remaining_turns…]
        return [{"role": "system", "content": _RAG_GROUNDING_RULES}] + sys_msgs + other_msgs
    return [{"role": "system", "content": _DEFAULT_RAG_SYSTEM_PROMPT}] + other_msgs
