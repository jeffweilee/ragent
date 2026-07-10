"""End-to-end: boot the MCP Hub as `python -m mcp_hub.server` in a
subprocess and drive it via real HTTP. The subprocess owns its own asyncio
loop, fully isolated from pytest-asyncio's function-scoped loop, so
uvicorn keep-alive workers and FastMCP's anyio task group can never leak
residue into the test runner and trigger "Event loop is closed" at CI
teardown.

Three scenarios:
- `tools/list` over Streamable HTTP returns the dot-qualified tool names.
- `tools/call` against a stub upstream returns the `{ok, status, data}` envelope.
- `GET /metrics` after a tool call exposes the per-tool Prometheus counter.

Two-system scenario:
- Hub loaded with two system YAML files exposes tools from both systems.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from pytest_httpserver import HTTPServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _serve(tools_dir: Path, extra_env: dict | None = None):
    """Spawn the Hub in a subprocess; yield its base URL."""
    port = _free_port()
    env = os.environ.copy()
    env["MCP_HUB_TOOLS_YAML"] = str(tools_dir)
    env["MCP_HUB_HOST"] = "127.0.0.1"
    env["MCP_HUB_PORT"] = str(port)
    env["MCP_HUB_PATH"] = "/mcp"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_hub.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as http:
            for _ in range(200):
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    raise RuntimeError(
                        f"hub subprocess exited early (rc={proc.returncode}): {stderr[-2000:]}"
                    )
                try:
                    resp = await http.get(f"{base_url}/metrics", timeout=0.5)
                    if resp.status_code == 200:
                        break
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("hub subprocess did not become ready within 10s")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _write_tools(tmp_path: Path, body: str, filename: str = "demo.yaml") -> Path:
    d = tmp_path / "tools.d"
    d.mkdir(exist_ok=True)
    (d / filename).write_text(textwrap.dedent(body))
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

    async with _serve(tools_dir) as base_url, Client(f"{base_url}/mcp/") as client:
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

    async with _serve(tools_dir) as base_url, Client(f"{base_url}/mcp/") as client:
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
    """`/metrics` is a Route sibling to `/mcp` on the Hub's Starlette app."""
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

    async with _serve(tools_dir) as base_url, Client(f"{base_url}/mcp/") as client:
        await client.call_tool("demo.ping", {})
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{base_url}/metrics")

    assert resp.status_code == 200
    body = resp.text
    assert "mcp_hub_tool_calls_total" in body
    assert 'system="demo"' in body
    assert 'tool="demo.ping"' in body
    assert 'outcome="success"' in body
    assert "mcp_hub_tool_call_duration_seconds" in body
    assert "mcp_hub_tool_load_failures_total" in body


@pytest.mark.asyncio
async def test_two_systems_both_exposed(tmp_path, httpserver: HTTPServer):
    """Hub loaded with two system YAML files exposes tools from both systems
    under their qualified names."""
    httpserver.expect_request("/ping", method="GET").respond_with_json({"pong": True})
    upstream = httpserver.url_for("").rstrip("/")

    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "alpha.yaml").write_text(
        textwrap.dedent(
            f"""\
            system: alpha
            defaults:
              base_url: {upstream}
              timeout: 5
            tools:
              - name: ping
                description: alpha ping
                method: GET
                path: /ping
            """
        )
    )
    (d / "beta.yaml").write_text(
        textwrap.dedent(
            f"""\
            system: beta
            defaults:
              base_url: {upstream}
              timeout: 5
            tools:
              - name: ping
                description: beta ping
                method: GET
                path: /ping
            """
        )
    )

    async with _serve(d) as base_url, Client(f"{base_url}/mcp/") as client:
        tools = await client.list_tools()

    names = {t.name for t in tools}
    assert "alpha.ping" in names
    assert "beta.ping" in names
