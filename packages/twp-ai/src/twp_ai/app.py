"""FastAPI wiring for twp-ai.

create_router() — returns an APIRouter; mount into any existing FastAPI app.
create_app()    — wraps the router in a standalone FastAPI app.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from .adapter import stream_chat_events
from .callers.protocol import LLMCaller
from .schemas import ChatRequest


def create_router(
    llm_caller: LLMCaller,
    default_model: str = "",
) -> APIRouter:
    """Return a router with POST /chat.

    Mount into ragent with a prefix, e.g.:
        app.include_router(create_router(caller), prefix="/twp/v1")
    → endpoint lives at POST /twp/v1/chat
    """
    router = APIRouter()

    @router.post("/chat")
    async def chat(body: ChatRequest) -> StreamingResponse:
        model = body.model or default_model

        def _generate():
            yield from stream_chat_events(body, model, llm_caller)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router


def create_app(
    llm_caller: LLMCaller,
    default_model: str = "",
) -> FastAPI:
    """Standalone FastAPI app — useful for running twp-ai as its own service."""
    _default_model = default_model or os.environ.get("TWP_DEFAULT_MODEL", "")
    app = FastAPI(title="twp-ai", version="0.1.0", description="AG-UI event streaming adapter")
    app.include_router(create_router(llm_caller, default_model=_default_model))
    return app
