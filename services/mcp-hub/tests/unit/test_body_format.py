"""body_format dispatch: json / form / multipart + type: file handling."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from mcp_hub.mcp_hub import _MAX_FILE_BYTES, _make_tool_callable, _parse_tool, load_tools_yaml


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _get_spec(raw: dict):
    return _parse_tool(raw)


@pytest.mark.asyncio
async def test_default_body_format_is_json():
    spec = _get_spec(
        {
            "name": "create",
            "method": "POST",
            "path": "https://api.example.com/items",
            "parameters": [
                {"name": "title", "type": "string", "location": "body", "required": True},
            ],
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["content_type"] = req.headers.get("content-type", "")
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn(title="hello")

    assert "application/json" in seen["content_type"]
    assert json.loads(seen["body"]) == {"title": "hello"}


@pytest.mark.asyncio
async def test_body_format_form_sends_urlencoded():
    spec = _get_spec(
        {
            "name": "submit",
            "method": "POST",
            "path": "https://api.example.com/submit",
            "body_format": "form",
            "parameters": [
                {"name": "field1", "type": "string", "location": "body", "required": True},
                {"name": "field2", "type": "integer", "location": "body", "required": True},
            ],
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["content_type"] = req.headers.get("content-type", "")
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn(field1="hello", field2=42)

    assert "application/x-www-form-urlencoded" in seen["content_type"]
    assert "field1=hello" in seen["body"]
    assert "field2=42" in seen["body"]


@pytest.mark.asyncio
async def test_body_format_multipart_non_file_sent_as_data():
    spec = _get_spec(
        {
            "name": "upload",
            "method": "POST",
            "path": "https://api.example.com/upload",
            "body_format": "multipart",
            "parameters": [
                {"name": "label", "type": "string", "location": "body", "required": True},
            ],
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["content_type"] = req.headers.get("content-type", "")
        seen["body"] = req.content.decode(errors="replace")
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn(label="test-label")

    assert "multipart/form-data" in seen["content_type"]
    assert "test-label" in seen["body"]


@pytest.mark.asyncio
async def test_body_format_multipart_file_param_decoded_and_sent():
    raw_bytes = b"fake file content"
    b64 = base64.b64encode(raw_bytes).decode()

    spec = _get_spec(
        {
            "name": "upload",
            "method": "POST",
            "path": "https://api.example.com/upload",
            "body_format": "multipart",
            "parameters": [
                {"name": "file", "type": "file", "location": "body", "required": True},
                {"name": "name", "type": "string", "location": "body", "required": True},
            ],
        }
    )
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["content_type"] = req.headers.get("content-type", "")
        seen["body"] = req.content
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    await fn(file=b64, name="doc.txt")

    assert "multipart/form-data" in seen["content_type"]
    assert raw_bytes in seen["body"]


@pytest.mark.asyncio
async def test_file_too_large_raises_tool_error():
    big_bytes = b"x" * (_MAX_FILE_BYTES + 1)
    b64 = base64.b64encode(big_bytes).decode()

    spec = _get_spec(
        {
            "name": "upload",
            "method": "POST",
            "path": "https://api.example.com/upload",
            "body_format": "multipart",
            "parameters": [
                {"name": "file", "type": "file", "location": "body", "required": True},
            ],
        }
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    with pytest.raises(ToolError) as exc_info:
        await fn(file=b64)

    payload = json.loads(str(exc_info.value))
    assert payload["type"] == "file_too_large"
    assert payload["max_bytes"] == _MAX_FILE_BYTES
    assert payload["received_bytes"] > _MAX_FILE_BYTES


@pytest.mark.asyncio
async def test_invalid_base64_raises_tool_error():
    spec = _get_spec(
        {
            "name": "upload",
            "method": "POST",
            "path": "https://api.example.com/upload",
            "body_format": "multipart",
            "parameters": [
                {"name": "file", "type": "file", "location": "body", "required": True},
            ],
        }
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler), "https://api.example.com")
    with pytest.raises(ToolError) as exc_info:
        await fn(file="not-valid-base64!!!")

    payload = json.loads(str(exc_info.value))
    assert payload["type"] == "invalid_base64"
    assert payload["param"] == "file"


def test_file_param_without_multipart_raises():
    """type: file requires body_format: multipart."""
    with pytest.raises(ValueError, match="multipart"):
        _parse_tool(
            {
                "name": "bad",
                "method": "POST",
                "path": "/upload",
                "body_format": "json",
                "parameters": [
                    {"name": "file", "type": "file", "location": "body", "required": True},
                ],
            }
        )


def test_file_param_not_in_body_raises():
    """type: file must have location: body."""
    with pytest.raises(ValueError, match="location: body"):
        _parse_tool(
            {
                "name": "bad",
                "method": "POST",
                "path": "/upload",
                "body_format": "multipart",
                "parameters": [
                    {"name": "file", "type": "file", "location": "query", "required": True},
                ],
            }
        )


def test_invalid_body_format_raises():
    with pytest.raises(ValueError, match="body_format"):
        _parse_tool(
            {
                "name": "bad",
                "method": "POST",
                "path": "/upload",
                "body_format": "xml",
                "parameters": [],
            }
        )


def test_body_format_loaded_from_yaml(tmp_path):
    yml = tmp_path / "tools.yaml"
    yml.write_text(
        "tools:\n"
        "  - name: form_submit\n"
        "    method: POST\n"
        "    path: https://api.example.com/submit\n"
        "    body_format: form\n"
        "    parameters:\n"
        "      - name: data\n"
        "        type: string\n"
        "        location: body\n"
        "        required: true\n"
    )
    result = load_tools_yaml(yml)
    assert result.tools[0].body_format == "form"
