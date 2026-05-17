"""ASGI middleware: populate the incoming-headers ContextVar from each HTTP
request so per-tool forward_headers can read it."""

from __future__ import annotations

import pytest

from ragent.mcp_hub.mcp_hub import _INCOMING_HEADERS
from ragent.mcp_hub.server import HeaderForwardMiddleware


@pytest.mark.asyncio
async def test_middleware_populates_contextvar_for_http():
    seen: dict = {}

    async def inner_app(scope, receive, send):
        seen["headers"] = dict(_INCOMING_HEADERS.get())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = HeaderForwardMiddleware(inner_app)
    scope = {
        "type": "http",
        "headers": [(b"x-user-id", b"u-42"), (b"x-request-id", b"r-7")],
    }
    sent: list = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    await mw(scope, receive, send)

    assert seen["headers"] == {"x-user-id": "u-42", "x-request-id": "r-7"}


@pytest.mark.asyncio
async def test_middleware_resets_contextvar_after_request():
    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = HeaderForwardMiddleware(inner_app)
    scope = {"type": "http", "headers": [(b"x-user-id", b"u-42")]}

    async def send(_msg):
        pass

    async def receive():
        return {"type": "http.request"}

    await mw(scope, receive, send)

    assert _INCOMING_HEADERS.get() is None


@pytest.mark.asyncio
async def test_middleware_passthrough_for_non_http_scope():
    received = []

    async def inner_app(scope, receive, send):
        received.append(scope["type"])

    mw = HeaderForwardMiddleware(inner_app)

    async def send(_msg):
        pass

    async def receive():
        return {}

    await mw({"type": "lifespan"}, receive, send)
    assert received == ["lifespan"]
