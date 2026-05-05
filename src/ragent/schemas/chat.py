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
