"""FastAPI wiring for twp-ai.

create_router(agent) — mount into any existing FastAPI app.
create_app(agent)    — standalone FastAPI app.

The agent controls the conversation flow and event emission.
Default: DirectLLMAgent (requires a LLMCaller).
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from .agent import Agent
from .schemas import RunAgentInput


def create_router(
    agent: Agent,
    default_model: str = "",
) -> APIRouter:
    """Return a router with POST /run wired to agent.run().

    Mount into ragent:
        app.include_router(create_router(agent), prefix="/twp/v1")
    → POST /twp/v1/run

    Swap agent:
        app.include_router(create_router(LangGraphAgent(graph)), prefix="/twp/v1")
    """
    router = APIRouter()

    @router.post("/run")
    async def run_agent(body: RunAgentInput) -> StreamingResponse:
        # The server owns the thread id: assign one when the client omits it so
        # every run has a concrete session id and RUN_STARTED.thread_id is never
        # null. (ragent's /chatagent/v3 mints its own Crockford id instead.)
        if body.thread_id is None:
            body = body.model_copy(update={"thread_id": uuid.uuid4().hex})
        model = body.model or default_model

        def _generate():
            yield from agent.run(body, model)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router


def create_app(
    agent: Agent,
    default_model: str = "",
) -> FastAPI:
    _default_model = default_model or os.environ.get("TWP_DEFAULT_MODEL", "")
    app = FastAPI(title="twp-ai", version="0.1.0", description="twp-ai event streaming adapter")
    app.include_router(create_router(agent, default_model=_default_model))
    return app
