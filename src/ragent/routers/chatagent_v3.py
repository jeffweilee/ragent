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
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from twp_ai.agents.adk import ADKAgent
from twp_ai.events import RunErrorEvent, to_sse
from twp_ai.schemas import RunAgentInput

from ragent.auth.deps import get_user_id
from ragent.clients.adk_caller import ADKCaller
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.routers._chatagent_proxy import proxy_get, proxy_write
from ragent.schemas.chatagent import SessionDeleteRequest, SessionRenameRequest
from ragent.services.chatagent_session import map_session_list_payload, map_session_payload
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)


def create_chatagent_v3_router(
    http_client: httpx.Client,
    chatagent_ap_name: str,
    chatagent_auth: str | None = None,
    chatagent_api_url: str | None = None,
    chatagent_sessionlist_api_url: str | None = None,
    chatagent_session_api_url: str | None = None,
    rate_limiter: RateLimiter | None = None,
    rate_limit: int = 60,
    rate_limit_window: int = 60,
    jwt_header: str = "X-Auth-Token",
    timeout: float = 30.0,
) -> APIRouter:
    router = APIRouter(prefix="/chatagent/v3")

    _headers: dict[str, str] = {"Authorization": chatagent_auth} if chatagent_auth else {}

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

            # Model B — ragent owns the session id. When the client omits it (a
            # brand-new conversation), mint one here so the upstream always
            # receives OUR session (it never mints its own) and the assigned id is
            # echoed back in RUN_STARTED for the client to reuse. Resolve before
            # any streaming path so RUN_STARTED / RUN_ERROR never carry a null id.
            if body.thread_id is None:
                body = body.model_copy(update={"thread_id": new_id()})

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

    if chatagent_sessionlist_api_url is not None:

        @router.get("/sessionList")
        async def chatagent_v3_session_list(
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
            startTime: str | None = None,
            endTime: str | None = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            params: dict[str, str] = {"user": user_id, "apName": chatagent_ap_name}
            if startTime:
                params["startTime"] = startTime
            if endTime:
                params["endTime"] = endTime
            # strip the machine-context wrapper from each session title.
            return await proxy_get(
                http_client=http_client,
                url=chatagent_sessionlist_api_url,
                params=params,
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.sessionlist",
                transform=map_session_list_payload,
            )

    if chatagent_session_api_url is not None:

        @router.get("/session")
        async def chatagent_v3_session(
            session: str,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            params = {"user": user_id, "apName": chatagent_ap_name, "session": session}
            # v3 reshapes the persisted history: twp-ai roles + <hidden> stripped.
            return await proxy_get(
                http_client=http_client,
                url=chatagent_session_api_url,
                params=params,
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session",
                transform=map_session_payload,
            )

        @router.put("/session")
        async def chatagent_v3_session_rename(
            body: SessionRenameRequest,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            return await proxy_write(
                http_client=http_client,
                method="PUT",
                url=chatagent_session_api_url,
                payload={
                    "session": body.session,
                    "sessionName": body.sessionName,
                    "apName": chatagent_ap_name,
                    "user": user_id,
                },
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session.rename",
            )

        @router.delete("/session")
        async def chatagent_v3_session_delete(
            body: SessionDeleteRequest,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            return await proxy_write(
                http_client=http_client,
                method="DELETE",
                url=chatagent_session_api_url,
                payload={"session": body.session, "apName": chatagent_ap_name, "user": user_id},
                headers=_headers,
                timeout=timeout,
                log_prefix="v3.session.delete",
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
