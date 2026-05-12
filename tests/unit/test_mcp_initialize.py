"""T-MCP.3 — Pin `initialize` handshake (S58).

Spec §3.8.2 / §3.8.1 / B47.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ragent
from ragent.routers.mcp import create_mcp_router


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router(retrieval_pipeline=MagicMock()))
    with TestClient(app) as c:
        yield c


def test_initialize_returns_pinned_protocol_version(client: TestClient) -> None:
    """S58 — initialize handshake returns the spec-pinned 2024-11-05 revision."""
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2024-11-05"


def test_initialize_advertises_tools_capability(client: TestClient) -> None:
    """S58 — server advertises `tools` capability (the retrieve tool is the
    sole tool exposed, per §3.8.3). No other capabilities (resources/prompts)
    in P2.5."""
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
    )
    caps = resp.json()["result"]["capabilities"]
    assert caps == {"tools": {}}


def test_initialize_server_info(client: TestClient) -> None:
    """S58 — serverInfo.name is the pinned `ragent` (B47); version matches
    ragent.__version__ (single source of truth)."""
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
    )
    info = resp.json()["result"]["serverInfo"]
    assert info["name"] == "ragent"
    assert info["version"] == ragent.__version__


def test_initialize_with_null_id_still_responds(client: TestClient) -> None:
    """Per JSON-RPC 2.0 §4.1, `id: null` is a request (not a notification);
    server MUST emit a response with id:null."""
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": None,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] is None
    assert "result" in body
