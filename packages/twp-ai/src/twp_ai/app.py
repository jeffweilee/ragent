"""FastAPI application factory for twp-ai."""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from .adapter import stream_chat_events
from .schemas import ChatRequest


def create_app() -> FastAPI:
    app = FastAPI(
        title="twp-ai",
        version="0.1.0",
        description="AG-UI style event streaming adapter",
    )

    llm_url: str = os.environ.get("TWP_LLM_URL", "")
    api_key: str = os.environ.get("TWP_LLM_API_KEY", "")
    default_model: str = os.environ.get("TWP_LLM_MODEL", "gpt-4o")

    @app.post("/chat")
    async def chat(body: ChatRequest) -> StreamingResponse:
        model = body.model or default_model

        def _generate():
            yield from stream_chat_events(body, model, llm_url, api_key)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return app
