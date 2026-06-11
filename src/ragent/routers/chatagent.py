"""T-CA — /chatagent/v1 proxy router (POST + GET sessionList + GET session)."""

from __future__ import annotations

import math
import time
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ragent.auth.deps import get_user_id
from ragent.clients.rate_limiter import RateLimiter
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.routers._chatagent_proxy import proxy_get, proxy_write, timeout_error, upstream_error
from ragent.schemas.chatagent import ChatAgentRequest, SessionDeleteRequest, SessionRenameRequest
from ragent.utility.id_gen import new_id

logger = structlog.get_logger(__name__)

_UPSTREAM_OK_CODE = 96200


def _rate_limit_response(reset_at: float) -> Response:
    retry_after = max(1, math.ceil(reset_at - time.time()))
    resp = problem(429, HttpErrorCode.CHATAGENT_RATE_LIMITED, "Too Many Requests")
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def create_chatagent_router(
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
    router = APIRouter(prefix="/chatagent/v1")

    _headers: dict[str, str] = {"Authorization": chatagent_auth} if chatagent_auth else {}

    def _check_rate(user_id: str | None) -> Response | None:
        if rate_limiter is None or user_id is None:
            return None
        result = rate_limiter.check(
            f"chatagent:{user_id}", limit=rate_limit, window_seconds=rate_limit_window
        )
        if not result.allowed:
            logger.warning(
                "chatagent.rate_limited",
                user_id=user_id,
                error_code=HttpErrorCode.CHATAGENT_RATE_LIMITED,
                http_status=429,
            )
            return _rate_limit_response(result.reset_at or 0)
        return None

    if chatagent_api_url is not None:

        @router.post("")
        async def chatagent_post(
            body: ChatAgentRequest,
            request: Request,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"

            if (blocked := _check_rate(x_user_id)) is not None:
                return blocked

            raw_token = request.headers.get(jwt_header.lower()) or ""
            last_user = next(
                (m["content"] for m in reversed(body.messages) if m.get("role") == "user"),
                "",
            )
            session_id = body.session or new_id()

            proxy_payload = {
                "metadata": {
                    "apName": chatagent_ap_name,
                    "session": session_id,
                    "user": user_id,
                    "userToken": raw_token,
                },
                "inputData": {"message": last_user},
                "stream": False,
            }

            try:
                resp = await run_in_threadpool(
                    http_client.post,
                    chatagent_api_url,
                    json=proxy_payload,
                    headers=_headers,
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.TimeoutException:
                logger.warning("chatagent.timeout", http_status=504)
                return timeout_error()
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
                logger.warning("chatagent.upstream_error", http_status=502)
                return upstream_error()

            return_code = data.get("returnCode") if isinstance(data, dict) else None
            if return_code != _UPSTREAM_OK_CODE:
                logger.warning(
                    "chatagent.bad_return_code", return_code=return_code, http_status=502
                )
                return upstream_error()

            return_data = data.get("returnData")
            messages = (return_data or {}).get("messages") or []
            if not messages:
                logger.warning("chatagent.empty_messages", http_status=502)
                return upstream_error()

            logger.info("chatagent.request", user_id=user_id, http_status=200)
            return JSONResponse(
                {
                    "session": session_id,
                    "content": messages[0]["content"],
                    "usage": {"promptTokens": None, "completionTokens": None},
                    "model": body.model,
                    "provider": body.provider,
                    "sources": None,
                }
            )

    async def _proxy_get(url: str, params: dict[str, str], log_prefix: str) -> Response:
        return await proxy_get(
            http_client=http_client,
            url=url,
            params=params,
            headers=_headers,
            timeout=timeout,
            log_prefix=log_prefix,
        )

    async def _proxy_write(method: str, url: str, payload: dict, log_prefix: str) -> Response:
        return await proxy_write(
            http_client=http_client,
            method=method,
            url=url,
            payload=payload,
            headers=_headers,
            timeout=timeout,
            log_prefix=log_prefix,
        )

    if chatagent_sessionlist_api_url is not None:

        @router.get("/sessionList")
        async def chatagent_session_list(
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
            return await _proxy_get(chatagent_sessionlist_api_url, params, "sessionlist")

    if chatagent_session_api_url is not None:

        @router.get("/session")
        async def chatagent_session(
            session: str,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            params = {"user": user_id, "apName": chatagent_ap_name, "session": session}
            return await _proxy_get(chatagent_session_api_url, params, "session")

        @router.put("/session")
        async def chatagent_session_rename(
            body: SessionRenameRequest,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            return await _proxy_write(
                "PUT",
                chatagent_session_api_url,
                {
                    "session": body.session,
                    "sessionName": body.sessionName,
                    "apName": chatagent_ap_name,
                    "user": user_id,
                },
                "session.rename",
            )

        @router.delete("/session")
        async def chatagent_session_delete(
            body: SessionDeleteRequest,
            x_user_id: Annotated[str | None, Depends(get_user_id)] = None,
        ) -> Response:
            user_id = x_user_id or "anonymous"
            return await _proxy_write(
                "DELETE",
                chatagent_session_api_url,
                {"session": body.session, "apName": chatagent_ap_name, "user": user_id},
                "session.delete",
            )

    return router
