"""End-to-end: boot the MCP Hub the same way `python -m ragent.mcp_hub.server`
does, drive it through FastMCP's Streamable HTTP client. Locks the wiring
between `build_hub` (yaml → tool registry) and `build_app` (FastMCP HTTP
app + lifespan + HeaderForwardMiddleware) so a regression in the boot path
fails CI instead of only showing up the first time an operator runs the
microservice.

Two scenarios:
- `tools/list` over Streamable HTTP returns the dot-qualified tool names
  the hub registered from yaml.
- `tools/call` against a stub upstream round-trips through the hub's httpx
  client and produces the canonical `{ok, status, data}` envelope.
"""

from __future__ import annotations

import asyncio
import socket
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client
from pytest_httpserver import HTTPServer

from ragent.mcp_hub.mcp_hub import build_hub
from ragent.mcp_hub.server import build_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _serve(tools_dir: Path):
    bundle = build_hub(tools_dir)
    app = build_app(bundle, path="/mcp")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("hub did not start within 10s")
        yield f"http://127.0.0.1:{port}/mcp/"
    finally:
        server.should_exit = True
        await task


def _write_tools(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "demo.yaml").write_text(textwrap.dedent(body))
    return d


@pytest.mark.asyncio
async def test_streamable_http_lists_tools(tmp_path):
    tools_dir = _write_tools(
        tmp_path,
        """\
        defaults:
          base_url: http://upstream.invalid
          timeout: 5
        tools:
          - name: ping
            description: ping
            method: GET
            path: /ping
          - name: pong
            description: pong
            method: POST
            path: /pong
        """,
    )

    async with _serve(tools_dir) as url, Client(url) as client:
        tools = await client.list_tools()

    assert {t.name for t in tools} == {"demo.ping", "demo.pong"}


@pytest.mark.asyncio
async def test_streamable_http_invokes_tool_against_upstream(tmp_path, httpserver: HTTPServer):
    httpserver.expect_request("/v1/echo", method="GET").respond_with_json({"pong": True})
    upstream = httpserver.url_for("").rstrip("/")
    tools_dir = _write_tools(
        tmp_path,
        f"""\
        defaults:
          base_url: {upstream}
          timeout: 5
        tools:
          - name: echo
            description: echo
            method: GET
            path: /v1/echo
        """,
    )

    async with _serve(tools_dir) as url, Client(url) as client:
        result = await client.call_tool("demo.echo", {})

    assert result.structured_content == {
        "ok": True,
        "status": 200,
        "data": {"pong": True},
    }


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_hub_counters_after_tool_call(
    tmp_path, httpserver: HTTPServer
):
    """`/metrics` is mounted sibling to `/mcp` on the same Starlette app.
    After a successful tool call the per-tool counter shows up in the
    Prometheus exposition body."""
    httpserver.expect_request("/v1/ping", method="GET").respond_with_json({"ok": True})
    upstream = httpserver.url_for("").rstrip("/")
    tools_dir = _write_tools(
        tmp_path,
        f"""\
        defaults:
          base_url: {upstream}
          timeout: 5
        tools:
          - name: ping
            description: ping
            method: GET
            path: /v1/ping
        """,
    )

    async with _serve(tools_dir) as url, Client(url) as client:
        await client.call_tool("demo.ping", {})
        # url ends with /mcp/; trim and hit /metrics on the same origin.
        metrics_url = url.rsplit("/mcp/", 1)[0] + "/metrics"
        async with httpx.AsyncClient() as http:
            resp = await http.get(metrics_url)

    assert resp.status_code == 200
    body = resp.text
    assert "mcp_hub_tool_calls_total" in body
    assert 'system="demo"' in body
    assert 'tool="demo.ping"' in body
    assert 'outcome="success"' in body
    assert "mcp_hub_tool_call_duration_seconds" in body
    assert "mcp_hub_tool_load_failures_total" in body
