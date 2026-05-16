"""Per-tool base_url override, static headers with env substitution, and
forward_headers pass-through (X-User-Id and friends)."""

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
async def test_static_headers_sent_on_each_request():
    spec = _parse_tool(
        {
            "name": "auth_tool",
            "method": "GET",
            "path": "/x",
            "static_headers": {"Authorization": "Bearer literal-token"},
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn()
    assert seen["headers"]["authorization"] == "Bearer literal-token"


def test_static_headers_resolve_env_at_load_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("API_B_TOKEN", "real-secret")
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    static_headers:\n"
        "      Authorization: 'Bearer ${API_B_TOKEN}'\n"
    )
    _, tools = load_tools_yaml(yml)
    assert tools[0].static_headers["Authorization"] == "Bearer real-secret"


def test_missing_env_var_fails_loud_at_load(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    static_headers:\n"
        "      Authorization: 'Bearer ${DEFINITELY_NOT_SET}'\n"
    )
    with pytest.raises(ValueError, match="DEFINITELY_NOT_SET"):
        load_tools_yaml(yml)


@pytest.mark.asyncio
async def test_forward_headers_propagate_from_contextvar():
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {"X-User-Id": "X-User-Id", "X-Trace-Id": "X-Request-Id"},
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
async def test_forward_headers_noop_when_contextvar_empty():
    spec = _parse_tool(
        {
            "name": "userful",
            "method": "GET",
            "path": "/me",
            "forward_headers": {"X-User-Id": "X-User-Id"},
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
        "      Authorization: 'Bearer literal'\n"
        "    parameters:\n"
        "      - name: authorization\n"
        "        type: string\n"
        "        location: header\n"
        "        required: true\n"
    )
    with pytest.raises(ValueError, match="authorization"):
        load_tools_yaml(yml)


def test_header_param_collides_with_forward_header_rejected(tmp_path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: t\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    forward_headers:\n"
        "      X-User-Id: X-Caller\n"
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
        "      X-User-Id: static-val\n"
        "    forward_headers:\n"
        "      X-User-Id: X-User-Id\n"
    )
    with pytest.raises(ValueError, match="x-user-id"):
        load_tools_yaml(yml)
