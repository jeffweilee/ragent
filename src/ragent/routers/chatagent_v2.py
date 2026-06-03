"""T-CAv2 — /chatagent/v2 raw-proxy router (POST with optional streaming)."""

from __future__ import annotations

import math
import time
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from ragent.auth.deps import get_user_id
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.schemas.chatagent import ChatAgentV2Request
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)


def _rate_limit_response(reset_at: float) -> Response:
    retry_after = max(1, math.ceil(reset_at - time.time()))
    resp = problem(429, HttpErrorCode.CHATAGENT_RATE_LIMITED, "Too Many Requests")
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def _upstream_error() -> Response:
    return problem(502, HttpErrorCode.CHATAGENT_UPSTREAM_ERROR, "Bad Gateway")


def _timeout_error() -> Response:
    return problem(504, HttpErrorCode.CHATAGENT_TIMEOUT, "Gateway Timeout")


def create_chatagent_v2_router(
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
    router = APIRouter(prefix="/chatagent/v2")

    _headers: dict[str, str] = {"Authorization": chatagent_auth} if chatagent_auth else {}

    def _check_rate(user_id: str | None) -> Response | None:
        if rate_limiter is None or user_id is None:
            return None
        result = rate_limiter.check(
            f"chatagent:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        if not result.allowed:
            logger.warning(
                "chatagent_v2.rate_limited",
                user_id=user_id,
                error_code=HttpErrorCode.CHATAGENT_RATE_LIMITED,
                http_status=429,
            )
            return _rate_limit_response(result.reset_at or 0)
        return None

    if chatagent_api_url is not None:

        @router.post("")
        async def chatagent_v2_post(
            body: ChatAgentV2Request,
            request: Request,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"

            if (blocked := _check_rate(x_user_id)) is not None:
                return blocked

            raw_token = request.headers.get(jwt_header.lower()) or ""
            session_id = body.metadata.session or new_id()

            upstream_payload = {
                "metadata": {
                    "apName": chatagent_ap_name,
                    "session": session_id,
                    "user": user_id,
                    "userToken": raw_token,
                },
                "inputData": {"message": body.inputData.message},
                "stream": body.stream,
            }

            # Validate status/headers before committing an HTTP status to the client.
            resp = None
            try:
                req = http_client.build_request(
                    "POST",
                    chatagent_api_url,
                    json=upstream_payload,
                    headers=_headers,
                    timeout=timeout,
                )
                resp = await run_in_threadpool(
                    http_client.send, req, stream=body.stream
                )
                resp.raise_for_status()
            except httpx.TimeoutException:
                if resp is not None:
                    resp.close()
                logger.warning("chatagent_v2.timeout", http_status=504)
                return _timeout_error()
            except (httpx.HTTPStatusError, httpx.RequestError):
                if resp is not None:
                    resp.close()
                logger.warning("chatagent_v2.upstream_error", http_status=502)
                return _upstream_error()

            content_type = resp.headers.get("content-type", "application/json")
            logger.info("chatagent_v2.request", user_id=user_id, http_status=200)

            if body.stream:

                def _gen():
                    try:
                        yield from resp.iter_bytes()
                    finally:
                        resp.close()

                return StreamingResponse(iterate_in_threadpool(_gen()), media_type=content_type)

            return Response(content=resp.content, media_type=content_type)

    return router
