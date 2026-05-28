"""Request/response schemas for the twp-ai chat endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    """Schema definition for a single tool (passed via context.tool_inputs)."""

    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    model_config = {"populate_by_name": True}


class ChatContext(BaseModel):
    """Extra context sent alongside the chat messages.

    tools:       names of tools the LLM may call this turn
    tool_inputs: per-tool configuration (currently: the JSON schema the LLM fills in)
    app_meta:    arbitrary key/value pairs injected into the system prompt
    """

    tools: list[str] = Field(default_factory=list)
    tool_inputs: dict[str, ToolInput] = Field(default_factory=dict)
    app_meta: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    context: ChatContext = Field(default_factory=ChatContext)
    model: str | None = None
