"""Each system gets its own httpx.AsyncClient (independent timeout + pool)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from ragent.mcp_hub.mcp_hub import build_hub


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).strip())
    return path


@pytest.mark.asyncio
async def test_per_system_client_carries_system_default_timeout(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "fast.yaml",
        """
        defaults:
          base_url: https://fast.example.com
          timeout: 5.0
        tools:
          - name: ping
            method: GET
            path: /ping
        """,
    )
    _write(
        d / "slow.yaml",
        """
        defaults:
          base_url: https://slow.example.com
          timeout: 60.0
        tools:
          - name: ping
            method: GET
            path: /ping
        """,
    )
    bundle = build_hub(d, name="t")

    assert "fast" in bundle.clients
    assert "slow" in bundle.clients
    assert bundle.clients["fast"].timeout.read == 5.0
    assert bundle.clients["slow"].timeout.read == 60.0

    for c in bundle.clients.values():
        await c.aclose()


@pytest.mark.asyncio
async def test_per_tool_timeout_overrides_system_default(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "api.yaml",
        """
        defaults:
          base_url: https://api.example.com
          timeout: 5.0
        tools:
          - name: heavy
            method: GET
            path: /heavy
            timeout: 120.0
        """,
    )
    bundle = build_hub(d, name="t")

    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["timeout"] = req.extensions.get("timeout")
        return httpx.Response(200, json={})

    bundle.clients["api"]._transport = httpx.MockTransport(handler)
    tool = await bundle.hub.get_tool("api.heavy")
    await tool.fn()

    assert seen["timeout"]["read"] == 120.0
    for c in bundle.clients.values():
        await c.aclose()


@pytest.mark.asyncio
async def test_systems_use_separate_clients(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "alpha.yaml",
        """
        defaults:
          base_url: https://alpha.example.com
        tools:
          - name: x
            method: GET
            path: /x
        """,
    )
    _write(
        d / "beta.yaml",
        """
        defaults:
          base_url: https://beta.example.com
        tools:
          - name: x
            method: GET
            path: /x
        """,
    )
    bundle = build_hub(d, name="t")

    assert bundle.clients["alpha"] is not bundle.clients["beta"]
    for c in bundle.clients.values():
        await c.aclose()
