"""Standalone entry point for the MCP Hub microservice.

Environment variables:
    MCP_HUB_TOOLS_YAML  Path to the tool registry (default: ./tools.yaml).
    MCP_HUB_NAME        Server name advertised to MCP clients.
    MCP_HUB_HOST        Bind host (default: 0.0.0.0).
    MCP_HUB_PORT        Bind port (default: 9000).
    MCP_HUB_PATH        Streamable HTTP mount path (default: /mcp).
    MCP_HUB_STATELESS_HTTP  Use stateless HTTP mode (default: false).
    MCP_HUB_JSON_RESPONSE   Return JSON responses instead of SSE (default: false).

Run:
    uvicorn ragent.mcp_hub.server:build_mcp_app --factory --host 0.0.0.0 --port 9000
    (legacy: uv run python -m ragent.mcp_hub.server)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ragent.utility.env import bool_env, int_env, str_env

from .mcp_hub import _INCOMING_HEADERS, HubBundle, build_hub

logger = structlog.get_logger(__name__)


class HeaderForwardMiddleware:
    """ASGI middleware that publishes each request's headers into a ContextVar
    so per-tool `forward_headers` can read them (X-User-Id, X-JWT-Token, etc.).

    SECURITY: This Hub trusts the incoming headers verbatim. Deploy behind
    mTLS or a trusted internal network so untrusted callers cannot forge
    these headers. The LLM must never be allowed to control these values —
    the MCP-client application (Haystack, your agent app) sets them in its
    transport, out-of-band from the model loop.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = _INCOMING_HEADERS.set(headers)
        try:
            await self.app(scope, receive, send)
        finally:
            _INCOMING_HEADERS.reset(token)


def build_app(
    bundle: HubBundle,
    *,
    path: str = "/mcp",
    json_response: bool = False,
    stateless_http: bool = False,
) -> Any:
    """Compose the ASGI app that `main()` serves so integration tests can boot
    the same code path without subprocessing. FastMCP 3.x owns the Streamable
    HTTP lifespan (session manager); we wrap it so our per-system httpx
    clients are closed on shutdown, then layer `HeaderForwardMiddleware` on
    the outside."""
    fastmcp_app = bundle.hub.http_app(
        path=path,
        transport="streamable-http",
        json_response=json_response,
        stateless_http=stateless_http,
    )

    async def _metrics(_request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # `Route` (not `Mount`) so `/metrics` serves directly without a 307 to
    # `/metrics/`. The FastMCP mount lives at `path` (default `/mcp`), so the
    # two never collide.
    fastmcp_app.router.routes.append(Route("/metrics", _metrics))
    fastmcp_lifespan = fastmcp_app.router.lifespan_context

    async def _close(system: str, client: Any) -> None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — one bad client must not leak others
            logger.error("mcp_hub.shutdown_error", system=system, exc_info=True)

    @asynccontextmanager
    async def composed(scope_app):
        async with fastmcp_lifespan(scope_app):
            try:
                yield
            finally:
                # gather so a stuck client doesn't block sibling cleanups.
                await asyncio.gather(*(_close(s, c) for s, c in bundle.clients.items()))

    fastmcp_app.router.lifespan_context = composed
    return HeaderForwardMiddleware(fastmcp_app)


def build_mcp_app() -> Any:
    """0-arg factory for ``uvicorn ragent.mcp_hub.server:build_mcp_app --factory``."""
    yaml_path = str_env("MCP_HUB_TOOLS_YAML", "tools.yaml")
    name = str_env("MCP_HUB_NAME", "ragent-mcp-hub")
    path = str_env("MCP_HUB_PATH", "/mcp")
    stateless_http = bool_env("MCP_HUB_STATELESS_HTTP", False)
    json_response = bool_env("MCP_HUB_JSON_RESPONSE", False)

    bundle = build_hub(yaml_path, name=name)
    return build_app(bundle, path=path, json_response=json_response, stateless_http=stateless_http)


def main() -> None:
    host = str_env("MCP_HUB_HOST", "0.0.0.0")
    port = int_env("MCP_HUB_PORT", 9000)
    log_level = str_env("LOG_LEVEL", "INFO").lower()
    # String factory form so each worker process calls build_mcp_app() independently,
    # avoiding shared httpx clients across fork boundaries.
    uvicorn.run(
        "ragent.mcp_hub.server:build_mcp_app",
        factory=True,
        host=host,
        port=port,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
