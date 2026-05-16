"""Error-handling contract for the MCP Hub:

- 2xx           -> {ok: True, status, data}
- 4xx           -> {ok: False, status, error: {...upstream body, truncated to 4KB...}}
- 5xx           -> raise ToolError (body redacted; only status + request_id leak out)
- timeout       -> raise ToolError
- connect error -> raise ToolError
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from ragent.mcp_hub.mcp_hub import _UPSTREAM_BODY_MAX_BYTES, _make_tool_callable, _parse_tool


def _get_spec() -> object:
    return _parse_tool(
        {
            "name": "get_user",
            "method": "GET",
            "path": "/users/{user_id}",
            "parameters": [
                {"name": "user_id", "type": "integer", "location": "path", "required": True},
            ],
        }
    )


def _post_spec() -> object:
    return _parse_tool(
        {
            "name": "create_order",
            "method": "POST",
            "path": "/orders",
            "parameters": [
                {"name": "sku", "type": "string", "location": "body", "required": True},
            ],
        }
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_2xx_returns_success_envelope():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 7, "name": "Ada"})

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    result = await fn(user_id=7)

    assert result == {"ok": True, "status": 200, "data": {"id": 7, "name": "Ada"}}


@pytest.mark.asyncio
async def test_4xx_returns_ok_false_with_upstream_body():
    body = {"code": "USER_NOT_FOUND", "detail": "user 7 does not exist"}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=body)

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    result = await fn(user_id=7)

    assert result["ok"] is False
    assert result["status"] == 404
    assert result["error"]["type"] == "upstream_4xx"
    assert result["error"]["upstream_body"] == body


@pytest.mark.asyncio
async def test_4xx_oversized_body_is_truncated():
    big = {"detail": "x" * (_UPSTREAM_BODY_MAX_BYTES + 1000)}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=big)

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    result = await fn(user_id=7)

    err = result["error"]
    assert err["truncated"] is True
    assert len(err["upstream_body"]) <= _UPSTREAM_BODY_MAX_BYTES


@pytest.mark.asyncio
async def test_4xx_html_body_is_dropped():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text="<html><body>forbidden</body></html>", headers={"content-type": "text/html"}
        )

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    result = await fn(user_id=7)

    err = result["error"]
    assert "upstream_body" not in err
    assert err["upstream_body_omitted"] is True
    assert err["upstream_content_type"] == "text/html"


@pytest.mark.asyncio
async def test_5xx_raises_tool_error_without_body():
    sensitive = {"sql": "SELECT * FROM users WHERE password='hunter2'"}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=sensitive)

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    with pytest.raises(ToolError) as ex:
        await fn(user_id=7)

    payload = json.loads(str(ex.value))
    assert payload["type"] == "upstream_5xx"
    assert payload["status"] == 500
    assert "hunter2" not in str(ex.value)
    assert "sql" not in payload


@pytest.mark.asyncio
async def test_5xx_passes_through_request_id():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"any": "secret"}, headers={"X-Request-Id": "abc-123"}
        )

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    with pytest.raises(ToolError) as ex:
        await fn(user_id=7)

    payload = json.loads(str(ex.value))
    assert payload["upstream_request_id"] == "abc-123"


@pytest.mark.asyncio
async def test_timeout_raises_tool_error():
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("upstream slow")

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    with pytest.raises(ToolError) as ex:
        await fn(user_id=7)

    payload = json.loads(str(ex.value))
    assert payload["type"] == "timeout"


@pytest.mark.asyncio
async def test_connect_error_raises_tool_error():
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    with pytest.raises(ToolError) as ex:
        await fn(user_id=7)

    payload = json.loads(str(ex.value))
    assert payload["type"] == "connect_error"


@pytest.mark.asyncio
async def test_4xx_text_plain_body_is_passed_through():
    """Plain-text 4xx bodies are surfaced (some APIs return text/plain errors)."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, text="invalid user_id", headers={"content-type": "text/plain; charset=utf-8"}
        )

    fn = _make_tool_callable(_get_spec(), _client(handler), "https://api.example.com")
    result = await fn(user_id=7)

    assert result["ok"] is False
    assert result["error"]["upstream_body"] == "invalid user_id"


@pytest.mark.asyncio
async def test_4xx_on_post_carries_body():
    """POST 4xx must also surface body so the LLM can self-correct request shape."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"errors": [{"field": "sku", "msg": "required"}]})

    fn = _make_tool_callable(_post_spec(), _client(handler), "https://api.example.com")
    result = await fn(sku="ABC")

    assert result["ok"] is False
    assert result["status"] == 422
    assert result["error"]["upstream_body"]["errors"][0]["field"] == "sku"
