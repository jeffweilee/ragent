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

import os
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn

from .mcp_hub import _INCOMING_HEADERS, build_hub

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

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            for system, client in bundle.clients.items():
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 — one bad client must not leak others
                    logger.error("mcp_hub.shutdown_error", system=system, exc_info=True)

    app = bundle.hub.http_app(path=path, lifespan=lifespan)
    uvicorn.run(HeaderForwardMiddleware(app), host=host, port=port)


if __name__ == "__main__":
    main()
