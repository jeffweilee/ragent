"""FastAPI wiring for twp-ai.

create_router(caller, handler) — returns an APIRouter to mount anywhere.
create_app(caller, handler)    — standalone FastAPI app.

The handler controls the conversation flow.
Default: handlers.form_fill.handle (chat + form fill).
Pass a different handler for a different scenario.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from .callers.protocol import LLMCaller
from .handlers import form_fill
from .schemas import ChatRequest

Handler = Callable[[ChatRequest, str, LLMCaller], Generator[str, None, None]]


def create_router(
    llm_caller: LLMCaller,
    handler: Handler = form_fill.handle,
    default_model: str = "",
) -> APIRouter:
    """Return a router with POST /chat wired to handler.

    Mount into ragent:
        app.include_router(create_router(caller), prefix="/twp/v1")
    → POST /twp/v1/chat

    Swap scenario:
        app.include_router(create_router(caller, handler=research.handle), prefix="/twp/v1")
    """
    router = APIRouter()

    @router.post("/chat")
    async def chat(body: ChatRequest) -> StreamingResponse:
        model = body.model or default_model

        def _generate():
            yield from handler(body, model, llm_caller)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router


def create_app(
    llm_caller: LLMCaller,
    handler: Handler = form_fill.handle,
    default_model: str = "",
) -> FastAPI:
    _default_model = default_model or os.environ.get("TWP_DEFAULT_MODEL", "")
    app = FastAPI(title="twp-ai", version="0.1.0", description="AG-UI event streaming adapter")
    app.include_router(create_router(llm_caller, handler=handler, default_model=_default_model))
    return app
