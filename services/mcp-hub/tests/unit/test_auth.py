"""Hub authentication middleware: custom header token guard.

Contracts:
- AuthMiddleware returns 401 JSON when token missing or wrong.
- AuthMiddleware strips the auth header from scope before inner app runs.
- /metrics path is exempt (Prometheus scraping).
- _validate_auth_forward_conflict sys.exit(1) when a tool's forward_headers
  references the auth header name.
"""

from __future__ import annotations

import json
import sys

import pytest

from mcp_hub.server import AuthMiddleware, _validate_auth_forward_conflict
from mcp_hub.mcp_hub import build_hub


def _make_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "path": path, "headers": headers}


@pytest.mark.asyncio
async def test_valid_token_passes_through():
    received = []

    async def inner_app(scope, receive, send):
        received.append("called")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = AuthMiddleware(inner_app, header="X-MCP-Hub-Token", token="secret")
    scope = _make_scope("/mcp", [(b"x-mcp-hub-token", b"secret")])

    sent = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, send, send)
    assert "called" in received


@pytest.mark.asyncio
async def test_missing_token_returns_401():
    async def inner_app(scope, receive, send):
        pytest.fail("inner_app must not be called on auth failure")

    mw = AuthMiddleware(inner_app, header="X-MCP-Hub-Token", token="secret")
    scope = _make_scope("/mcp", [])

    sent = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, send, send)

    start = sent[0]
    assert start["status"] == 401
    body_msg = sent[1]
    payload = json.loads(body_msg["body"])
    assert payload["error"] == "missing_or_invalid_token"
    assert payload["expected_header"] == "X-MCP-Hub-Token"


@pytest.mark.asyncio
async def test_wrong_token_returns_401():
    async def inner_app(scope, receive, send):
        pytest.fail("inner_app must not be called on auth failure")

    mw = AuthMiddleware(inner_app, header="X-MCP-Hub-Token", token="correct")
    scope = _make_scope("/mcp", [(b"x-mcp-hub-token", b"wrong")])

    sent = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, send, send)

    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_auth_header_stripped_before_inner_app():
    """Auth header must not reach inner app (prevents forwarding to upstream)."""
    seen_headers: list = []

    async def inner_app(scope, receive, send):
        seen_headers.extend(scope.get("headers", []))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = AuthMiddleware(inner_app, header="X-MCP-Hub-Token", token="s3cr3t")
    scope = _make_scope(
        "/mcp",
        [
            (b"x-mcp-hub-token", b"s3cr3t"),
            (b"x-user-id", b"u-42"),
        ],
    )

    sent = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, send, send)

    header_names = [k.lower() for k, _ in seen_headers]
    assert b"x-mcp-hub-token" not in header_names
    assert b"x-user-id" in header_names


@pytest.mark.asyncio
async def test_metrics_path_exempt_from_auth():
    """Prometheus scraper must reach /metrics without a token."""
    received = []

    async def inner_app(scope, receive, send):
        received.append("called")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = AuthMiddleware(inner_app, header="X-MCP-Hub-Token", token="secret")
    scope = _make_scope("/metrics", [])

    sent = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, send, send)

    assert "called" in received
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_non_http_scope_bypasses_auth():
    received = []

    async def inner_app(scope, receive, send):
        received.append(scope["type"])

    mw = AuthMiddleware(inner_app, header="X-MCP-Hub-Token", token="secret")

    async def noop(*_):
        pass

    await mw({"type": "lifespan"}, noop, noop)
    assert received == ["lifespan"]


def test_validate_auth_forward_conflict_exits_on_conflict(tmp_path, monkeypatch):
    """If any tool's forward_headers references the auth header, startup must
    abort rather than silently failing to forward it."""
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "bad.yaml").write_text(
        "tools:\n"
        "  - name: leaky\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    forward_headers:\n"
        "      X-MCP-Hub-Token: '{x-mcp-hub-token}'\n"
    )
    bundle = build_hub(d, name="t")

    exit_calls = []
    monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))

    _validate_auth_forward_conflict(bundle, "X-MCP-Hub-Token")

    assert exit_calls == [1]


def test_validate_auth_forward_conflict_ok_when_no_conflict(tmp_path):
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "ok.yaml").write_text(
        "tools:\n"
        "  - name: safe\n"
        "    method: GET\n"
        "    path: https://api.example.com/x\n"
        "    forward_headers:\n"
        "      X-User-Id: '{x-user-id}'\n"
    )
    bundle = build_hub(d, name="t")
    _validate_auth_forward_conflict(bundle, "X-MCP-Hub-Token")
