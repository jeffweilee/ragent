"""T-CAUI.4 — `tools/call AGENTIC_UI_TOOL` is rejected server-side.

The dispatcher is a client-side tool: the frontend executes it. A server-side
`tools/call` for it must return a soft `isError` result (NOT run retrieval and
NOT a JSON-RPC error envelope), so a misrouted call degrades gracefully.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router


@pytest.fixture
def pipeline() -> MagicMock:
    return MagicMock()


@pytest.fixture
def client(pipeline: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router(retrieval_pipeline=pipeline))
    return TestClient(app)


def test_tools_call_agentic_ui_returns_iserror_without_running_retrieval(
    client: TestClient, pipeline: MagicMock
) -> None:
    body = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "AGENTIC_UI_TOOL",
                "arguments": {"tool_name": "fill_form", "arguments": {"description": "x"}},
            },
        },
    ).json()

    # Soft error result, not a JSON-RPC error envelope.
    assert "error" not in body
    result = body["result"]
    assert result["isError"] is True
    assert "client-side" in result["content"][0]["text"].lower()
    # The retrieval pipeline is never touched for a client-side dispatch.
    pipeline.run.assert_not_called()
