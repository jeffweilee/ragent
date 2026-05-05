"""T3.4 — ChatRequest schema with env defaults and filter validation (B12, B21, B22, B29)."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field, field_validator

_DEFAULT_PROVIDER = os.environ.get("RAGENT_DEFAULT_LLM_PROVIDER", "openai")
_DEFAULT_MODEL = os.environ.get("RAGENT_DEFAULT_LLM_MODEL", "gptoss-120b")
_DEFAULT_TEMPERATURE = float(os.environ.get("RAGENT_DEFAULT_TEMPERATURE", "0.7"))
_DEFAULT_MAX_TOKENS = int(os.environ.get("RAGENT_DEFAULT_MAX_TOKENS", "4096"))
_DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "RAGENT_DEFAULT_SYSTEM_PROMPT", "You are a helpful assistant"
)
_DEFAULT_RAG_SYSTEM_PROMPT = os.environ.get(
    "RAGENT_DEFAULT_RAG_SYSTEM_PROMPT",
    """\
You are a retrieval-grounded assistant. Every user turn contains an \
isolated `=== CONTEXT START === ... === CONTEXT END ===` block followed \
by the user's request. Use ONLY facts from that block; do not rely on \
prior knowledge. If the context is insufficient, reply exactly: \
"I don't know based on the provided context."

At most one citation per paragraph, placed at the paragraph end as \
[source_title] (or [document_id] if title is missing). Do not cite \
mid-sentence. Citations must refer only to entries in the context block.

---
Detect the user's intent and respond in the matching style:

1. QUESTION  — the user asks something answerable from the context.
   Style: direct, 1–4 sentences, lead with the answer. One citation at \
the end of the paragraph.
   Example:
     User: "When did Acme launch v2?"
     Context: [#1] source_title=Acme Wiki "...Acme v2 shipped on 2024-03-12..."
     Assistant: "Acme v2 shipped on 2024-03-12. [Acme Wiki]"

2. SUMMARY  — the user asks to summarise / overview / "tl;dr" the context.
   Style: 3–6 bullet points. Each bullet is one fact; place the citation \
at the end of the bullet if the source differs from the previous bullet, \
otherwise omit to avoid repetition. No preamble.
   Example:
     User: "Summarise the onboarding doc."
     Context: [#1] source_title=Onboarding "...step1...step2...step3..."
     Assistant:
       - Account provisioning is the first step. [Onboarding]
       - SSO enrolment follows provisioning.
       - First-login walkthrough completes onboarding.

3. GENERATION — the user asks to draft / write / compose new text grounded \
in the context (e.g. "draft a release note from these tickets").
   Style: produce the requested artefact in natural prose or the format \
requested. Place at most one citation at the end of each paragraph. \
Do not invent facts absent from the context.
   Example:
     User: "Draft a one-line changelog entry."
     Context: [#1] source_title=PR-482 "...fixes login retry loop..."
     Assistant: "Fixed an infinite retry loop on failed logins. [PR-482]"

If the request fits none of the above, default to QUESTION style.""",
)
_RAG_GROUNDING_RULES = os.environ.get(
    "RAGENT_RAG_GROUNDING_RULES",
    """\
Use ONLY facts from the `=== CONTEXT START === ... === CONTEXT END ===` \
block in the user turn; do not rely on prior knowledge. If the context \
is insufficient, reply exactly: \
"I don't know based on the provided context."

At most one citation per paragraph, placed at the paragraph end as \
[source_title] (or [document_id] if title is missing). Do not cite \
mid-sentence. Citations must refer only to entries in the context block.

Detect the user's intent (QUESTION / SUMMARY / GENERATION) and apply \
the matching response style: direct answer, bullet-point summary, or \
drafted artefact. Default to QUESTION style when intent is unclear.""",
)
_PROVIDER_ALLOWLIST = frozenset({"openai"})
_FILTER_MAX_LEN = 64


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    provider: str = _DEFAULT_PROVIDER
    model: str = _DEFAULT_MODEL
    temperature: float = _DEFAULT_TEMPERATURE
    max_tokens: int = _DEFAULT_MAX_TOKENS
    source_app: str | None = None
    source_workspace: str | None = None

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in _PROVIDER_ALLOWLIST:
            raise ValueError(f"provider must be one of {sorted(_PROVIDER_ALLOWLIST)}")
        return v

    @field_validator("source_app", "source_workspace", mode="before")
    @classmethod
    def _validate_filter(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v == "" or len(v) > _FILTER_MAX_LEN:
            raise ValueError(f"filter field must be 1–{_FILTER_MAX_LEN} chars")
        return v


def normalize_messages(req: ChatRequest) -> list[dict[str, Any]]:
    has_system = any(m.get("role") == "system" for m in req.messages)
    if has_system:
        return list(req.messages)
    return [{"role": "system", "content": _DEFAULT_SYSTEM_PROMPT}] + list(req.messages)


def _render_context(docs: list[Any]) -> str:
    parts = []
    for i, doc in enumerate(docs, start=1):
        meta = getattr(doc, "meta", None) or {}
        parts.append(
            f"[#{i}] source_app={meta.get('source_app', 'unknown')} "
            f"source_title={meta.get('source_title', 'unknown')} "
            f"document_id={meta.get('document_id', 'unknown')}\n"
            f"{getattr(doc, 'content', '') or ''}\n---"
        )
    return "\n".join(parts)


def _wrap_last_user(messages: list[dict[str, Any]], context_block: str) -> list[dict[str, Any]]:
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            original = out[i].get("content", "") or ""
            out[i] = {
                **out[i],
                "content": (
                    f"=== CONTEXT START ===\n{context_block}\n=== CONTEXT END ===\n\n{original}"
                ),
            }
            return out
    return out


def build_rag_messages(req: ChatRequest, docs: list[Any] | None) -> list[dict[str, Any]]:
    base = list(req.messages)
    has_user_system = any(m.get("role") == "system" for m in base)
    if not docs:
        if has_user_system:
            return base
        return [{"role": "system", "content": _DEFAULT_SYSTEM_PROMPT}] + base
    context_block = _render_context(docs)
    wrapped = _wrap_last_user(base, context_block)
    if has_user_system:
        return [{"role": "system", "content": _RAG_GROUNDING_RULES}] + wrapped
    return [{"role": "system", "content": _DEFAULT_RAG_SYSTEM_PROMPT}] + wrapped
