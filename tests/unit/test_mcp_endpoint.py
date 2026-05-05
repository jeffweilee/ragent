"""T6.2 — POST /mcp/tools/rag returns 501 MCP_NOT_IMPLEMENTED in P1 (S8)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(create_mcp_router())
    with TestClient(app) as c:
        yield c


def test_rag_tool_returns_501(client):
    resp = client.post("/mcp/tools/rag")
    assert resp.status_code == 501


def test_rag_tool_returns_problem_json(client):
    resp = client.post("/mcp/tools/rag")
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_rag_tool_error_code(client):
    resp = client.post("/mcp/tools/rag")
    assert resp.json()["error_code"] == "MCP_NOT_IMPLEMENTED"
