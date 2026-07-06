"""Generic authenticated reverse proxy for the ragent-brain management surface.

Fronts brain's entire ``/upstream/*`` API through ragent under
``/brainagent/v1/{path}`` → brain ``{brain_url}/upstream/{path}``, so the whole
brain management surface (session / memory / projects / sources / artifacts /
skills / preferences / schedules) is reachable via ragent and any new brain
``/upstream/*`` route is covered automatically.

Security-critical: the caller's ``user`` is forced to the JWT-resolved identity
in BOTH the query string and (when the body is a JSON object) the body,
overriding any client-supplied value — a client can never read or mutate another
user's data by forging ``user``. ``X-Brain-Key`` is attached server-to-server.

Responses are relayed verbatim: brain's ``422 {"error", "params"}`` i18n
envelope and binary artifact downloads (bytes + ``Content-Type`` /
``Content-Disposition``) both pass through unchanged. Only transport failures are
mapped — timeout → 504, unreachable → 502.

The run surface (``POST /brainagent/v1``, ``/reconnect``, ``/runs/{id}/cancel``)
is handled by `brainagent.py`; its router is mounted FIRST so those explicit
routes win over this catch-all.
"""

from __future__ import annotations

import json

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool

from ragent.auth.deps import get_user_id
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem

logger = structlog.get_logger(__name__)

# Headers we do NOT forward from brain's response — hop-by-hop / length framing
# that httpx already accounts for; re-sending them would corrupt the relay.
_STRIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}

# brain /upstream paths that must NOT be reachable through the user-authenticated
# proxy. `reindex` is a server-to-server admin rebuild (requires brain's
# X-Brain-Admin-Key); the spec excludes it from this surface, so a user-scoped
# request must never reach it. Returned as 404 (the route does not exist here).
_DENIED_PATHS = {"reindex"}


def create_brain_upstream_proxy_router(
    http_client: httpx.Client,
    *,
    brain_url: str,
    brain_key: str | None = None,
    timeout: float = 30.0,
) -> APIRouter:
    router = APIRouter(prefix="/brainagent/v1")
    base = brain_url.rstrip("/")

    def _upstream_headers(user_id: str) -> dict[str, str]:
        headers = {"X-User-Id": user_id}
        if brain_key:
            headers["X-Brain-Key"] = brain_key
        return headers

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(
        path: str,
        request: Request,
        x_user_id: str | None = Depends(get_user_id),
    ) -> Response:
        user_id = x_user_id or "anonymous"
        if path.strip("/") in _DENIED_PATHS:
            return Response(status_code=404)
        url = f"{base}/upstream/{path}"

        # Force user = resolved caller in the query string (brain reads ?user= on
        # GET/DELETE), overriding any forged client value. multi_items() preserves
        # repeated keys (?tag=a&tag=b); dict() would collapse them to the last one.
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "user"]
        params.append(("user", user_id))

        # …and in the JSON body (brain reads body.user on POST/PUT), same override.
        raw_body = await request.body()
        json_body: dict | None = None
        if raw_body:
            try:
                parsed = json.loads(raw_body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                parsed["user"] = user_id
                json_body = parsed

        headers = _upstream_headers(user_id)
        # Forward content negotiation from the client so binary/artifact downloads
        # negotiate correctly at brain. (Content-Type is forwarded only on the
        # raw-body path below; the json= path lets httpx set application/json.)
        if "accept" in request.headers:
            headers["Accept"] = request.headers["accept"]
        try:
            if json_body is not None:
                resp = await run_in_threadpool(
                    http_client.request,
                    request.method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
            elif raw_body:
                # Non-JSON body — forward the raw bytes unchanged, preserving the
                # client's Content-Type so brain can parse them (e.g. multipart).
                if "content-type" in request.headers:
                    headers["Content-Type"] = request.headers["content-type"]
                resp = await run_in_threadpool(
                    http_client.request,
                    request.method,
                    url,
                    params=params,
                    content=raw_body,
                    headers=headers,
                    timeout=timeout,
                )
            else:
                resp = await run_in_threadpool(
                    http_client.request,
                    request.method,
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                )
        except httpx.TimeoutException:
            logger.warning("brainagent.proxy.timeout", path=path, http_status=504)
            return problem(504, HttpErrorCode.BRAINAGENT_TIMEOUT, "Gateway Timeout")
        except httpx.RequestError:
            logger.warning("brainagent.proxy.upstream_error", path=path, http_status=502)
            return problem(502, HttpErrorCode.BRAINAGENT_UPSTREAM_ERROR, "Bad Gateway")

        # Relay status + body VERBATIM — brain's 422 i18n envelope and binary
        # artifact downloads both pass through untouched. Do not raise_for_status.
        passthrough = {
            k: v for k, v in resp.headers.items() if k.lower() not in _STRIP_RESPONSE_HEADERS
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=passthrough,
            media_type=resp.headers.get("content-type"),
        )

    return router
