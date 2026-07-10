"""Entry point for the MCP Hub microservice.

Environment variables:
    MCP_HUB_TOOLS_YAML      Path to tool registry dir or file (default: ./tools.yaml).
    MCP_HUB_NAME            Server name advertised to MCP clients.
    MCP_HUB_HOST            Bind host (default: 0.0.0.0).
    MCP_HUB_PORT            Bind port (default: 9000).
    MCP_HUB_PATH            Streamable HTTP mount path (default: /mcp).
    MCP_HUB_STATELESS_HTTP  Stateless HTTP mode (default: false).
    MCP_HUB_JSON_RESPONSE   JSON responses instead of SSE (default: false).
    MCP_HUB_AUTH_HEADER     Header name for hub auth (default: X-MCP-Hub-Token).
    MCP_HUB_AUTH_TOKEN      Expected token value; unset = auth disabled.

Run:
    uvicorn mcp_hub.server:build_mcp_app --factory --host 0.0.0.0 --port 9000
    uv run python -m mcp_hub.server
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ._env import bool_env, int_env, str_env
from .doctor import check_yaml
from .mcp_hub import _INCOMING_HEADERS, _TEMPLATE_PLACEHOLDER, HubBundle, build_hub

logger = structlog.get_logger(__name__)


class HeaderForwardMiddleware:
    """Publishes each request's headers into _INCOMING_HEADERS ContextVar.

    SECURITY: This middleware trusts incoming headers verbatim. Deploy behind
    mTLS or a trusted internal network. The LLM must never control these
    values — the MCP-client application sets them in its transport layer,
    out-of-band from the model loop.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        token = _INCOMING_HEADERS.set(headers)
        try:
            await self.app(scope, receive, send)
        finally:
            _INCOMING_HEADERS.reset(token)


class AuthMiddleware:
    """Validates a shared token in a custom header before forwarding to the hub.

    Uses a non-Authorization header name so callers can still forward user
    credentials (e.g. JWT) via forward_headers without name collision.

    The auth header is stripped from the request before HeaderForwardMiddleware
    runs, so it never enters _INCOMING_HEADERS and cannot be forwarded upstream.

    /metrics is exempt so Prometheus can scrape without credentials.
    """

    def __init__(self, app: Any, *, header: str, token: str) -> None:
        self.app = app
        self._header_lower = header.lower().encode()
        self._header_name = header
        self._token = token.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return

        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        token_value = b""
        stripped: list[tuple[bytes, bytes]] = []
        for k, v in raw_headers:
            if k.lower() == self._header_lower:
                token_value = v
            else:
                stripped.append((k, v))

        if not hmac.compare_digest(token_value, self._token):
            logger.warning(
                "mcp_hub.auth_rejected",
                reason="missing or invalid token",
                expected_header=self._header_name,
                path=scope.get("path"),
            )
            body = json.dumps(
                {
                    "error": "missing_or_invalid_token",
                    "expected_header": self._header_name,
                }
            ).encode()
            resp_headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ]
            await send({"type": "http.response.start", "status": 401, "headers": resp_headers})
            await send({"type": "http.response.body", "body": body})
            return

        scope = {**scope, "headers": stripped}
        await self.app(scope, receive, send)


def build_app(
    bundle: HubBundle,
    *,
    path: str = "/mcp",
    json_response: bool = False,
    stateless_http: bool = False,
    auth_header: str = "X-MCP-Hub-Token",
    auth_token: str | None = None,
) -> Any:
    """Compose the ASGI app. Integration tests use this directly."""
    fastmcp_app = bundle.hub.http_app(
        path=path,
        transport="streamable-http",
        json_response=json_response,
        stateless_http=stateless_http,
    )

    async def _metrics(_request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    fastmcp_app.router.routes.append(Route("/metrics", _metrics))
    fastmcp_lifespan = fastmcp_app.router.lifespan_context

    async def _close(system: str, client: Any) -> None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.error("mcp_hub.shutdown_error", system=system, exc_info=True)

    @asynccontextmanager
    async def composed(scope_app):
        async with fastmcp_lifespan(scope_app):
            try:
                yield
            finally:
                await asyncio.gather(*(_close(s, c) for s, c in bundle.clients.items()))

    fastmcp_app.router.lifespan_context = composed

    app: Any = HeaderForwardMiddleware(fastmcp_app)
    if auth_token:
        app = AuthMiddleware(app, header=auth_header, token=auth_token)
    return app


def _validate_auth_forward_conflict(bundle: HubBundle, auth_header: str) -> None:
    """Ensure no tool's forward_headers references the auth header.

    The auth header is stripped by AuthMiddleware before HeaderForwardMiddleware
    runs, so any forward_headers template referencing it would always skip
    (silent miss). Fail loudly at startup rather than silently.
    """
    auth_lower = auth_header.lower()
    for tool in bundle.tools:
        for tmpl in tool.forward_headers.values():
            refs = {m.group(1) for m in _TEMPLATE_PLACEHOLDER.finditer(tmpl)}
            if auth_lower in refs:
                logger.error(
                    "mcp_hub.config_error",
                    tool=tool.name,
                    reason=(
                        f"forward_headers references auth header {auth_header!r} which "
                        f"is stripped by AuthMiddleware and will never be available"
                    ),
                )
                sys.exit(1)


def _is_load_failure_error(err: str) -> bool:
    """True when a check_yaml error came from a parse/IO failure (file-path prefix).

    Static analysis errors use tool names as prefix (e.g. 'billing.list_charges: ...')
    which never contain path separators. LoadFailure and raw_errors use file paths.
    """
    prefix = err.split(":")[0]
    return "/" in prefix or "\\" in prefix


def build_mcp_app() -> Any:
    """0-arg factory for ``uvicorn mcp_hub.server:build_mcp_app --factory``."""
    yaml_path = str_env("MCP_HUB_TOOLS_YAML", "tools.yaml")
    name = str_env("MCP_HUB_NAME", "ragent-mcp-hub")
    path = str_env("MCP_HUB_PATH", "/mcp")
    stateless_http = bool_env("MCP_HUB_STATELESS_HTTP", False)
    json_response = bool_env("MCP_HUB_JSON_RESPONSE", False)
    auth_header = str_env("MCP_HUB_AUTH_HEADER", "X-MCP-Hub-Token")
    auth_token = str_env("MCP_HUB_AUTH_TOKEN", "") or None

    if not auth_token:
        logger.warning(
            "mcp_hub.auth_disabled",
            reason="MCP_HUB_AUTH_TOKEN not set; hub accepts unauthenticated requests",
        )

    # Static analysis pre-check: catch errors where every call would fail (path
    # placeholder mismatches, non-identifier param names, body on wrong method).
    # parse/IO failures are fault-isolated by build_hub(strict=False) below —
    # they surface as bundle.failures (warnings) not as a hard exit.
    errors, _ = check_yaml(yaml_path, placeholder_ok=True)
    static_errors = [e for e in errors if not _is_load_failure_error(e)]
    if static_errors:
        for err in static_errors:
            logger.error("mcp_hub.config_invalid", detail=err)
        sys.exit(1)

    bundle = build_hub(yaml_path, name=name, env=os.environ)

    for f in bundle.failures:
        logger.warning("mcp_hub.system_skipped", source=f.source, reason=f.reason, phase=f.phase)
    logger.info(
        "mcp_hub.config_ok",
        tool_count=len(bundle.tools),
        skipped_systems=len(bundle.failures),
        path=yaml_path,
    )

    if auth_token:
        _validate_auth_forward_conflict(bundle, auth_header)

    return build_app(
        bundle,
        path=path,
        json_response=json_response,
        stateless_http=stateless_http,
        auth_header=auth_header,
        auth_token=auth_token,
    )


def main() -> None:
    host = str_env("MCP_HUB_HOST", "0.0.0.0")
    port = int_env("MCP_HUB_PORT", 9000)
    log_level = str_env("LOG_LEVEL", "INFO").lower()
    uvicorn.run(
        "mcp_hub.server:build_mcp_app",
        factory=True,
        host=host,
        port=port,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
