"""Shared upstream-proxy helpers for the ChatAgent session routes.

`/chatagent/v1` and `/chatagent/v3` expose the same session-management surface
(sessionList + session GET/PUT/DELETE) over the same upstream service; only the
v3 `GET /session` reshapes the response (twp-ai roles + hidden stripped) via a
`transform` callback. The request plumbing — threadpool dispatch, status check,
and the timeout→504 / error→502 mapping — is identical, so it lives here once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import structlog
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem

logger = structlog.get_logger(__name__)


def upstream_error() -> Response:
    return problem(502, HttpErrorCode.CHATAGENT_UPSTREAM_ERROR, "Bad Gateway")


def timeout_error() -> Response:
    return problem(504, HttpErrorCode.CHATAGENT_TIMEOUT, "Gateway Timeout")


async def proxy_get(
    *,
    http_client: httpx.Client,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    timeout: float,
    log_prefix: str,
    transform: Callable[[Any], Any] | None = None,
) -> Response:
    try:
        resp = await run_in_threadpool(
            http_client.get, url, params=params, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        payload = resp.json()
    except httpx.TimeoutException:
        logger.warning("chatagent.proxy.timeout", route=log_prefix, http_status=504)
        return timeout_error()
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
        logger.warning("chatagent.proxy.upstream_error", route=log_prefix, http_status=502)
        return upstream_error()
    if transform is not None:
        payload = transform(payload)
    return JSONResponse(payload)


async def proxy_write(
    *,
    http_client: httpx.Client,
    method: str,
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float,
    log_prefix: str,
) -> Response:
    try:
        resp = await run_in_threadpool(
            http_client.request, method, url, json=payload, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return Response(status_code=resp.status_code)
        return JSONResponse(resp.json())
    except httpx.TimeoutException:
        logger.warning("chatagent.proxy.timeout", route=log_prefix, http_status=504)
        return timeout_error()
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
        logger.warning("chatagent.proxy.upstream_error", route=log_prefix, http_status=502)
        return upstream_error()
