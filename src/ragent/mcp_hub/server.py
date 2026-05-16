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

import uvicorn

from .mcp_hub import build_hub


def main() -> None:
    yaml_path = os.environ.get("MCP_HUB_TOOLS_YAML", "tools.yaml")
    name = os.environ.get("MCP_HUB_NAME", "ragent-mcp-hub")
    host = os.environ.get("MCP_HUB_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_HUB_PORT", "9000"))
    path = os.environ.get("MCP_HUB_PATH", "/mcp")

    hub, client = build_hub(yaml_path, name=name)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await client.aclose()

    app = hub.http_app(path=path, lifespan=lifespan)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
