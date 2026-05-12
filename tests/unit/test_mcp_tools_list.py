"""T-MCP.5 — Pin `tools/list` contract (S59) — exactly one tool `retrieve`
with inputSchema matching spec §3.8.3 verbatim.
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


def test_tools_list_returns_exactly_one_tool(client: TestClient) -> None:
    resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    tools = body["result"]["tools"]
    assert isinstance(tools, list)
    assert len(tools) == 1


def test_tools_list_deep_equals_spec_literal(client: TestClient) -> None:
    """S59 plan deliverable: inputSchema deep-equals the spec §3.8.3 literal.

    Catches additions (e.g. a new property slipping in) that per-field tests
    miss; pinned by importing the module constant so a drift between code
    and spec surfaces here as a separate failure.
    """
    from ragent.routers.mcp import _RETRIEVE_TOOL_SCHEMA

    [tool] = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()["result"]["tools"]
    assert tool == _RETRIEVE_TOOL_SCHEMA


def test_tools_list_advertises_retrieve_tool(client: TestClient) -> None:
    resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    [tool] = resp.json()["result"]["tools"]
    assert tool["name"] == "retrieve"
    assert isinstance(tool["description"], str)
    assert "retriev" in tool["description"].lower()  # description mentions retrieval


def test_retrieve_input_schema_required_fields(client: TestClient) -> None:
    """S59 — `query` is the only required input field per §3.8.3."""
    [tool] = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()["result"]["tools"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]


def test_retrieve_input_schema_property_types(client: TestClient) -> None:
    """S59 — each property in §3.8.3 has the documented type + bounds."""
    [tool] = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()["result"]["tools"]
    props = tool["inputSchema"]["properties"]

    assert props["query"]["type"] == "string"
    assert props["query"]["minLength"] == 1

    assert props["top_k"]["type"] == "integer"
    assert props["top_k"]["minimum"] == 1
    assert props["top_k"]["maximum"] == 200
    assert props["top_k"]["default"] == 20

    assert props["source_app"]["type"] == "string"
    assert props["source_app"]["maxLength"] == 64

    assert props["source_meta"]["type"] == "string"
    assert props["source_meta"]["maxLength"] == 1024

    assert props["min_score"]["type"] == "number"
    assert props["min_score"]["minimum"] == 0

    assert props["dedupe"]["type"] == "boolean"
    assert props["dedupe"]["default"] is False
