"""Per-tool base_url override, literal static_headers, and forward_headers
where the VALUE is a template substituting incoming MCP-client headers."""

from __future__ import annotations

import httpx
import pytest

from ragent.mcp_hub.mcp_hub import (
    _INCOMING_HEADERS,
    _make_tool_callable,
    _parse_tool,
    load_tools_yaml,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_per_tool_base_url_overrides_default():
    spec = _parse_tool(
        {
            "name": "get_b",
            "method": "GET",
            "path": "/me",
            "base_url": "https://api-b.example.com",
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api-a.example.com")
    await fn()
    assert seen["url"] == "https://api-b.example.com/me"


@pytest.mark.asyncio
async def test_falls_back_to_default_base_url_when_per_tool_absent():
    spec = _parse_tool({"name": "get_a", "method": "GET", "path": "/x"})
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api-a.example.com")
    await fn()
    assert seen["url"] == "https://api-a.example.com/x"


@pytest.mark.asyncio
async def test_static_headers_sent_as_literal():
    spec = _parse_tool(
        {
            "name": "static_tool",
            "method": "GET",
            "path": "/x",
            "static_headers": {"X-API-Version": "2", "Accept": "application/json"},
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn()
    assert seen["headers"]["x-api-version"] == "2"
    assert seen["headers"]["accept"] == "application/json"


def test_static_headers_no_longer_resolve_env_refs(tmp_path):
    """`${VAR}` is treated as a literal string — env-var substitution removed."""
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    static_headers:\n"
        "      X-API-Version: '${UNRESOLVED}'\n"
    )
    _, tools = load_tools_yaml(yml)
    assert tools[0].static_headers["X-API-Version"] == "${UNRESOLVED}"


@pytest.mark.asyncio
async def test_forward_template_substitutes_incoming_value():
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {
                "X-User-Id": "{x-user-id}",
                "X-Request-Id": "{x-trace-id}",
            },
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    token = _INCOMING_HEADERS.set({"x-user-id": "u-42", "x-trace-id": "t-7"})
    try:
        await fn()
    finally:
        _INCOMING_HEADERS.reset(token)

    assert seen["headers"]["x-user-id"] == "u-42"
    assert seen["headers"]["x-request-id"] == "t-7"


@pytest.mark.asyncio
async def test_forward_template_wraps_with_prefix():
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {"Authorization": "Bearer {x-jwt-token}"},
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    token = _INCOMING_HEADERS.set({"x-jwt-token": "eyJabc"})
    try:
        await fn()
    finally:
        _INCOMING_HEADERS.reset(token)

    assert seen["headers"]["authorization"] == "Bearer eyJabc"


@pytest.mark.asyncio
async def test_forward_template_combines_multiple_incoming():
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {
                "X-Audit-Ctx": "user={x-user-id};tenant={x-tenant-id}",
            },
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    token = _INCOMING_HEADERS.set({"x-user-id": "u-42", "x-tenant-id": "acme"})
    try:
        await fn()
    finally:
        _INCOMING_HEADERS.reset(token)

    assert seen["headers"]["x-audit-ctx"] == "user=u-42;tenant=acme"


@pytest.mark.asyncio
async def test_forward_template_skipped_when_placeholder_missing():
    """If any {placeholder} has no matching incoming header, skip the entire
    outgoing header (graceful degradation)."""
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {
                "X-User-Id": "{x-user-id}",
                "X-Tenant": "{x-tenant-id}",
            },
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    token = _INCOMING_HEADERS.set({"x-user-id": "u-42"})
    try:
        await fn()
    finally:
        _INCOMING_HEADERS.reset(token)

    assert seen["headers"]["x-user-id"] == "u-42"
    assert "x-tenant" not in seen["headers"]


@pytest.mark.asyncio
async def test_forward_template_with_no_placeholders_sent_as_literal():
    """A forward_headers value with no {placeholders} is sent verbatim
    regardless of incoming headers — degenerate but well-defined."""
    spec = _parse_tool(
        {
            "name": "literal",
            "method": "GET",
            "path": "/x",
            "forward_headers": {"X-Audit-Source": "ragent-hub"},
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn()

    assert seen["headers"]["x-audit-source"] == "ragent-hub"


@pytest.mark.asyncio
async def test_forward_template_noop_when_contextvar_empty():
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {"X-User-Id": "{x-user-id}"},
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn()

    assert "x-user-id" not in seen["headers"]


def test_header_param_collides_with_static_header_rejected(tmp_path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    static_headers:\n"
        "      X-API-Version: '2'\n"
        "    parameters:\n"
        "      - name: x_api_version\n"
        "        type: string\n"
        "        location: header\n"
        "        required: true\n"
    )
    with pytest.raises(ValueError, match="x-api-version"):
        load_tools_yaml(yml)


def test_header_param_collides_with_forward_header_rejected(tmp_path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    forward_headers:\n"
        "      X-Caller: '{x-user-id}'\n"
        "    parameters:\n"
        "      - name: x_caller\n"
        "        type: string\n"
        "        location: header\n"
        "        required: true\n"
    )
    with pytest.raises(ValueError, match="x-caller"):
        load_tools_yaml(yml)


def test_overlap_static_and_forward_rejected(tmp_path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    static_headers:\n"
        "      X-User-Id: 'static-val'\n"
        "    forward_headers:\n"
        "      X-User-Id: '{x-user-id}'\n"
    )
    with pytest.raises(ValueError, match="x-user-id"):
        load_tools_yaml(yml)
