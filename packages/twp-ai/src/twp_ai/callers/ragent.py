"""RagentCaller — wraps ragent's LLMClient to satisfy the LLMCaller Protocol.

No runtime import of ragent occurs here; the llm_client is injected from
ragent's composition root, keeping twp-ai free of a hard ragent dependency.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from .protocol import ToolDef

if TYPE_CHECKING:
    from ragent.clients.llm import LLMClient


def _to_openai_tool(tool: ToolDef) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.schema,
        },
    }


class RagentCaller:
    """Delegates streaming to ragent's LLMClient.stream_with_tools().

    Converts ToolDef → OpenAI function-calling format; the underlying
    LLM API is OpenAI-compatible so no further translation is needed.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._client = llm_client

    def stream_events(
        self,
        messages: list[dict],
        tools: list[ToolDef],
        model: str,
    ) -> Generator[tuple[str, Any], None, None]:
        openai_tools = [_to_openai_tool(t) for t in tools]
        yield from self._client.stream_with_tools(messages, openai_tools, model)
