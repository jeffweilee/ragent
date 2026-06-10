"""T-CAv3 — /chatagent/v3 router (twp-ai protocol proxy over the v2 upstream).

Accepts a twp-ai `RunAgentInput`, proxies to `CHATAGENT_API_URL` (shared with
v2), and streams the upstream response back as a twp-ai SSE event stream. All
failures — rate-limit, upstream error, timeout — surface as a `RUN_ERROR` event
over a 200 stream, never as an HTTP 4xx/5xx code.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from twp_ai.agents.adk import ADKAgent
from twp_ai.events import RunErrorEvent, to_sse
from twp_ai.schemas import RunAgentInput

from ragent.auth.deps import get_user_id
from ragent.clients.adk_caller import ADKCaller
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode

logger = structlog.get_logger(__name__)


def create_chatagent_v3_router(
    http_client: httpx.Client,
    chatagent_ap_name: str,
    chatagent_auth: str | None = None,
    chatagent_api_url: str | None = None,
    rate_limiter: RateLimiter | None = None,
    rate_limit: int = 60,
    rate_limit_window: int = 60,
    jwt_header: str = "X-Auth-Token",
    timeout: float = 30.0,
) -> APIRouter:
    router = APIRouter(prefix="/chatagent/v3")

    def _rate_limited(user_id: str | None) -> bool:
        if rate_limiter is None or user_id is None:
            return False
        result = rate_limiter.check(
            f"chatagent:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        return not result.allowed

    if chatagent_api_url is not None:

        @router.post("")
        async def chatagent_v3_post(
            body: RunAgentInput,
            request: Request,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> StreamingResponse:
            user_id = x_user_id or "anonymous"

            if _rate_limited(x_user_id):
                logger.warning(
                    "chatagent_v3.rate_limited",
                    user_id=user_id,
                    error_code=HttpErrorCode.CHATAGENT_RATE_LIMITED,
                )
                return StreamingResponse(_rate_limit_stream(body), media_type="text/event-stream")

            raw_token = request.headers.get(jwt_header.lower()) or ""
            caller = ADKCaller(
                http_client=http_client,
                api_url=chatagent_api_url,
                ap_name=chatagent_ap_name,
                user_id=user_id,
                user_token=raw_token,
                auth=chatagent_auth,
                timeout=timeout,
            )
            agent = ADKAgent(caller)
            logger.info("chatagent_v3.request", user_id=user_id)

            return StreamingResponse(
                agent.run(body, body.model or ""), media_type="text/event-stream"
            )

    return router


def _rate_limit_stream(body: RunAgentInput) -> Generator[str, None, None]:
    yield to_sse(
        RunErrorEvent(
            message="Too Many Requests",
            code=HttpErrorCode.CHATAGENT_RATE_LIMITED,
            run_id=body.run_id,
            thread_id=body.thread_id,
        )
    )
