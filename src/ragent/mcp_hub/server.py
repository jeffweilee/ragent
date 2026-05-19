"""Standalone entry point for the MCP Hub microservice.

Environment variables:
    MCP_HUB_TOOLS_YAML  Path to the tool registry (default: ./tools.yaml).
    MCP_HUB_NAME        Server name advertised to MCP clients.
    MCP_HUB_HOST        Bind host (default: 0.0.0.0).
    MCP_HUB_PORT        Bind port (default: 9000).
    MCP_HUB_PATH        Streamable HTTP mount path (default: /mcp).

Run:
    uv run python -m ragent.mcp_hub.server
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

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


def build_app(bundle: HubBundle, *, path: str = "/mcp") -> Any:
    """Compose the ASGI app that `main()` serves so integration tests can boot
    the same code path without subprocessing. FastMCP 3.x owns the Streamable
    HTTP lifespan (session manager); we wrap it so our per-system httpx
    clients are closed on shutdown, then layer `HeaderForwardMiddleware` on
    the outside."""
    fastmcp_app = bundle.hub.http_app(path=path)

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


def main() -> None:
    yaml_path = os.environ.get("MCP_HUB_TOOLS_YAML", "tools.yaml")
    name = os.environ.get("MCP_HUB_NAME", "ragent-mcp-hub")
    host = os.environ.get("MCP_HUB_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("MCP_HUB_PORT", "9000"))
    except ValueError as exc:
        raise SystemExit(f"MCP_HUB_PORT must be an integer, got {exc.args[0]!r}") from exc
    path = os.environ.get("MCP_HUB_PATH", "/mcp")

    bundle = build_hub(yaml_path, name=name)
    uvicorn.run(build_app(bundle, path=path), host=host, port=port)


if __name__ == "__main__":
    main()
