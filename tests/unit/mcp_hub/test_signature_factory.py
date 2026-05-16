"""Validate the dynamic signature factory — the critical contract for FastMCP
schema inference."""

from __future__ import annotations

import inspect
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from ragent.mcp_hub.mcp_hub import (
    _make_tool_callable,
    _parse_tool,
    build_hub,
    load_tools_yaml,
)


def _spec(raw: dict) -> object:
    return _parse_tool(raw)


def test_required_param_has_no_default_and_strict_type():
    spec = _spec(
        {
            "name": "get_user",
            "method": "GET",
            "path": "/users/{user_id}",
            "parameters": [
                {"name": "user_id", "type": "integer", "location": "path", "required": True},
            ],
        }
    )
    fn = _make_tool_callable(spec, httpx.AsyncClient(), "https://x")
    sig = inspect.signature(fn)
    p = sig.parameters["user_id"]
    assert p.annotation is int
    assert p.default is inspect.Parameter.empty
    assert fn.__annotations__["user_id"] is int


def test_optional_param_is_union_with_none_and_has_default():
    spec = _spec(
        {
            "name": "search",
            "method": "GET",
            "path": "/s",
            "parameters": [
                {"name": "limit", "type": "integer", "required": False, "default": 20},
                {"name": "flag", "type": "boolean", "required": False},
            ],
        }
    )
    fn = _make_tool_callable(spec, httpx.AsyncClient(), "https://x")
    sig = inspect.signature(fn)
    assert sig.parameters["limit"].default == 20
    assert sig.parameters["limit"].annotation == (int | None)
    assert sig.parameters["flag"].default is None
    assert sig.parameters["flag"].annotation == (bool | None)


def test_unsupported_type_raises():
    with pytest.raises(ValueError, match="unsupported type"):
        _parse_tool(
            {
                "name": "bad",
                "method": "GET",
                "path": "/",
                "parameters": [{"name": "x", "type": "bigint"}],
            }
        )


def test_load_yaml_round_trip(tmp_path: Path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        dedent(
            """
            defaults:
              base_url: https://api.example.com
            tools:
              - name: ping
                method: GET
                path: /ping
            """
        ).strip()
    )
    defaults, tools = load_tools_yaml(yml)
    assert defaults["base_url"] == "https://api.example.com"
    assert [t.name for t in tools] == ["ping"]


@pytest.mark.asyncio
async def test_request_dispatch_routes_params_by_location():
    spec = _spec(
        {
            "name": "create_order",
            "method": "POST",
            "path": "/users/{user_id}/orders",
            "parameters": [
                {"name": "user_id", "type": "integer", "location": "path", "required": True},
                {"name": "sku", "type": "string", "location": "body", "required": True},
                {
                    "name": "quantity",
                    "type": "integer",
                    "location": "body",
                    "required": False,
                    "default": 1,
                },
                {"name": "x_tenant", "type": "string", "location": "header", "required": True},
                {
                    "name": "dry_run",
                    "type": "boolean",
                    "location": "query",
                    "required": False,
                    "default": False,
                },
            ],
        }
    )

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["content"] = request.content.decode() if request.content else ""
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    fn = _make_tool_callable(spec, client, "https://api.example.com")

    result = await fn(user_id=7, sku="ABC", quantity=3, x_tenant="t1", dry_run=True)
    assert result == {"status": 200, "data": {"ok": True}}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.example.com/users/7/orders?dry_run=true"
    assert seen["headers"]["x-tenant"] == "t1"
    assert '"sku":"ABC"' in seen["content"].replace(" ", "")
    assert '"quantity":3' in seen["content"].replace(" ", "")


@pytest.mark.asyncio
async def test_build_hub_registers_every_yaml_entry(tmp_path: Path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        dedent(
            """
            defaults:
              base_url: https://api.example.com
            tools:
              - name: a
                method: GET
                path: /a
              - name: b
                method: GET
                path: /b
                parameters:
                  - name: q
                    type: string
                    required: true
            """
        ).strip()
    )
    hub, client = build_hub(yml, name="test-hub")
    tools = await hub.list_tools()
    assert {t.name for t in tools} == {"a", "b"}
    await client.aclose()
