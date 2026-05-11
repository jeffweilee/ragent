"""T-MCP.1 — Pin JSON-RPC 2.0 envelope contract for POST /mcp/v1.

Covers BDD scenarios S61 (method not found), S64 (parse error), S65
(notifications/initialized). Spec §3.8.1 / §3.8.4 / B47.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router())
    with TestClient(app) as c:
        yield c


def test_parse_error_returns_minus_32700_with_null_id(client: TestClient) -> None:
    """S64 — malformed JSON body → 200 with JSON-RPC error code -32700 and id:null.

    Per JSON-RPC 2.0 §5, if the request id could not be parsed (e.g. invalid
    JSON), the response id MUST be null.
    """
    resp = client.post(
        "/mcp/v1",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == -32700
    assert body["error"]["data"]["error_code"] == "MCP_PARSE_ERROR"


def test_invalid_request_missing_jsonrpc_returns_minus_32600(client: TestClient) -> None:
    """Body without `jsonrpc:"2.0"` is an Invalid Request (-32600)."""
    resp = client.post("/mcp/v1", json={"id": 1, "method": "ping"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["error"]["code"] == -32600
    assert body["error"]["data"]["error_code"] == "MCP_INVALID_REQUEST"


def test_invalid_request_missing_method_returns_minus_32600(client: TestClient) -> None:
    """Body without `method` is an Invalid Request (-32600)."""
    resp = client.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32600
    assert body["error"]["data"]["error_code"] == "MCP_INVALID_REQUEST"


def test_method_not_found_returns_minus_32601(client: TestClient) -> None:
    """S61 — unknown method outside §3.8.2 allow-list → -32601."""
    resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 7, "method": "resources/list"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert body["error"]["data"]["error_code"] == "MCP_METHOD_NOT_FOUND"


def test_notification_returns_204_with_empty_body(client: TestClient) -> None:
    """S65 — request without `id` is a JSON-RPC notification; server emits no
    response object. HTTP 204 with empty body is the canonical transport mapping.
    """
    resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert resp.status_code == 204
    assert resp.content == b""


def test_unknown_notification_also_returns_204(client: TestClient) -> None:
    """Notifications with unrecognised method name still produce no response
    body — JSON-RPC 2.0 §4.1 forbids responding to any notification regardless
    of whether the method is known.
    """
    resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "method": "notifications/cancelled"},
    )
    assert resp.status_code == 204
    assert resp.content == b""


def test_response_content_type_is_application_json_for_success(client: TestClient) -> None:
    """JSON-RPC responses use plain application/json (not problem+json which
    is reserved for transport-layer 401/4xx per §3.8.1)."""
    resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
