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
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool

from ragent.auth.deps import get_forwarded_headers, get_user_id
from ragent.clients.brain_caller import SERVICE_HEADER_NAMES, build_brain_headers
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
    pat_service: Any = None,
    pat_header_name: str = "X-Pat-Token",
) -> APIRouter:
    router = APIRouter(prefix="/brainagent/v1")
    base = brain_url.rstrip("/")

    # A misconfigured PAT_UPSTREAM_HEADER_NAME that collides with a service-owned
    # header (X-User-Id / X-Brain-Key) would overwrite the caller identity or the
    # brain secret AFTER build_brain_headers set them — refuse to attach under
    # such a name (Codex review r3619473865).
    _pat_header_safe = pat_header_name.lower() not in SERVICE_HEADER_NAMES
    if pat_service is not None and not _pat_header_safe:
        logger.error(
            "brainagent.proxy.pat_header_rejected",
            pat_header_name=pat_header_name,
            reason="collides with a service-owned header",
        )

    def _upstream_headers(user_id: str, forwarded: dict[str, str] | None) -> dict[str, str]:
        # Service-owned X-User-Id / X-Brain-Key always win over any same-named
        # (case-insensitive) forwarded header — a forged value cannot cross
        # tenants or spoof the secret. ``None`` forwarded is handled safely.
        return build_brain_headers(user_id, brain_key, forwarded)

    async def _attach_pat(headers: dict[str, str], user_id: str) -> None:
        # T-PAT.12 — attach the caller's resolved PAT for the upstream (drive tool
        # reached through brain). FAIL-OPEN: no PAT / invalid / redis miss → the
        # header is simply absent and the proxy behaves exactly as before. The PAT
        # slice is off entirely when pat_service is None (PAT_PUBLIC_KEY unset).
        if pat_service is None or not _pat_header_safe:
            return
        try:
            token = await pat_service.resolve_best_effort(user_id)
        except Exception:  # noqa: BLE001 — attach is additive; never break the proxy
            logger.warning("brainagent.proxy.pat_attach_failed", user_id=user_id)
            return
        # Drop any case-variant of the PAT header a client smuggled in via a
        # forwarded-header allowlist BEFORE setting ours — otherwise httpx would
        # emit two `X-Pat-Token` lines (different dict keys) and brain could read
        # the forged one. The server-resolved PAT must be the sole value.
        for existing in [h for h in headers if h.lower() == pat_header_name.lower()]:
            del headers[existing]
        if token:
            headers[pat_header_name] = token

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(
        path: str,
        request: Request,
        x_user_id: str | None = Depends(get_user_id),
        forwarded_headers: Annotated[dict[str, str], Depends(get_forwarded_headers)] = None,
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

        headers = _upstream_headers(user_id, forwarded_headers)
        await _attach_pat(headers, user_id)
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
