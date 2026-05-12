"""T-MCP.9 — Pin all `tools/call` error paths (S62, S63, S67).

Spec §3.8.4 / §3.8.5. Covers:
- S62 unknown tool name → -32602 data.error_code=MCP_TOOL_NOT_FOUND
- S63 inputSchema violation (missing query, top_k out of bounds, etc.)
       → -32602 data.error_code=MCP_TOOL_INPUT_INVALID
- S67 pipeline raises → -32001 data.error_code=MCP_TOOL_EXECUTION_FAILED
       (JSON-RPC error envelope, NOT result.isError=true)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router


@pytest.fixture
def app() -> FastAPI:
    pipeline = MagicMock()
    a = FastAPI()
    a.include_router(create_mcp_router(retrieval_pipeline=pipeline))
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _call(client: TestClient, name: str, arguments: dict, req_id: int = 1) -> dict:
    return client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ).json()


def test_tools_call_unknown_tool_name(client: TestClient) -> None:
    """S62 — unknown tool → -32602 MCP_TOOL_NOT_FOUND."""
    body = _call(client, name="search", arguments={"query": "q"})
    assert "result" not in body
    err = body["error"]
    assert err["code"] == -32602
    assert err["data"]["error_code"] == "MCP_TOOL_NOT_FOUND"


@pytest.mark.parametrize(
    "arguments, constraint",
    [
        ({}, "missing required `query`"),
        ({"query": ""}, "`query` violates minLength:1"),
        ({"query": 42}, "`query` violates type:string"),
        ({"query": "q", "top_k": 0}, "`top_k` violates minimum:1"),
        ({"query": "q", "top_k": 201}, "`top_k` violates maximum:200"),
        ({"query": "q", "top_k": "ten"}, "`top_k` violates type:integer"),
    ],
)
def test_tools_call_input_schema_violations(
    client: TestClient, arguments: dict, constraint: str
) -> None:
    """S63 — each §3.8.3 inputSchema constraint failure → -32602 MCP_TOOL_INPUT_INVALID.

    `constraint` is the human-readable label of the constraint being pinned;
    one row per distinct constraint so a test name still identifies which
    §3.8.3 rule a future regression breaks.
    """
    body = _call(client, name="retrieve", arguments=arguments)
    assert "result" not in body, constraint
    err = body["error"]
    assert err["code"] == -32602, constraint
    assert err["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID", constraint


def test_tools_call_pipeline_failure(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """S67 — pipeline raises → JSON-RPC error envelope with -32001 and
    MCP_TOOL_EXECUTION_FAILED. NOT a success response with isError:true
    (which would be reserved for a soft-error future case).
    """

    def _boom(*_a, **_kw):
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr("ragent.routers.mcp.run_retrieval", _boom)
    client = TestClient(app)
    body = _call(client, name="retrieve", arguments={"query": "q"})
    assert "result" not in body
    err = body["error"]
    assert err["code"] == -32001
    assert err["data"]["error_code"] == "MCP_TOOL_EXECUTION_FAILED"


def test_tools_call_missing_params(client: TestClient) -> None:
    """`params` absent from envelope → -32602 (tools/call requires `name`)."""
    body = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
    ).json()
    err = body["error"]
    assert err["code"] == -32602
    assert err["data"]["error_code"] == "MCP_TOOL_NOT_FOUND"
