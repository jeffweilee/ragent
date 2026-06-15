"""T-MCP.5 / T-CAUI.4 — Pin the `tools/list` contract (S59): the `retrieve` tool
with inputSchema matching spec §3.8.3 verbatim, plus the `AGENTIC_UI_TOOL`
client-side dispatcher.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_mcp_router(retrieval_pipeline=MagicMock()))
    with TestClient(app) as c:
        yield c


def _tools(client: TestClient) -> list[dict]:
    return client.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()[
        "result"
    ]["tools"]


def _tool(client: TestClient, name: str) -> dict:
    return next(t for t in _tools(client) if t["name"] == name)


def test_tools_list_returns_registered_tools(client: TestClient) -> None:
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
    assert {t["name"] for t in tools} == {"retrieve", "AGENTIC_UI_TOOL"}


def test_tools_list_deep_equals_model_dump(client: TestClient) -> None:
    """S59: the retrieve entry deep-equals RETRIEVE_TOOL.model_dump(exclude_none=True).

    Pins the wire format against the mcp.types.Tool descriptor derived from
    _RetrieveArgs, so any drift between the registry and the serialised response
    surfaces here as a failure rather than silently reaching MCP clients.
    """
    from ragent.routers.mcp_tools.retrieve import RETRIEVE_TOOL

    assert _tool(client, "retrieve") == RETRIEVE_TOOL.model_dump(exclude_none=True)


def test_tools_list_advertises_retrieve_tool(client: TestClient) -> None:
    tool = _tool(client, "retrieve")
    assert tool["name"] == "retrieve"
    assert isinstance(tool["description"], str)
    assert "retriev" in tool["description"].lower()  # description mentions retrieval


def test_tools_list_advertises_agentic_ui_tool(client: TestClient) -> None:
    """T-CAUI.4 — the client-side dispatcher is advertised with its envelope schema."""
    tool = _tool(client, "AGENTIC_UI_TOOL")
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["tool_name", "arguments"]
    assert schema["properties"]["tool_name"]["type"] == "string"
    assert schema["properties"]["arguments"]["type"] == "object"


def test_retrieve_input_schema_required_fields(client: TestClient) -> None:
    """S59 — `query` is the only required input field per §3.8.3."""
    tool = _tool(client, "retrieve")
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]


def test_retrieve_input_schema_property_types(client: TestClient) -> None:
    """S59 — each property in §3.8.3 has the documented type + bounds."""
    tool = _tool(client, "retrieve")
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


def test_retrieve_optional_fields_have_no_null_default(client: TestClient) -> None:
    """Optional fields must not advertise default:null in inputSchema.

    After collapsing anyOf:[{type:T},{type:null}] → {type:T}, a lingering
    default:null is contradictory — null is not a valid value for type:T.
    An MCP client that materialises the advertised default and submits null
    would receive -32602 before retrieval runs.  Optionality is expressed via
    absence from "required", not via default:null.
    """
    tool = _tool(client, "retrieve")
    props = tool["inputSchema"]["properties"]
    for name in ("source_app", "source_meta", "min_score"):
        assert props[name].get("default") is not None or "default" not in props[name], (
            f"inputSchema.properties.{name} must not have default:null "
            f"(null is not a valid value once the null type branch is collapsed)"
        )


def test_retrieve_input_schema_all_properties_have_descriptions(client: TestClient) -> None:
    """Every inputSchema property must carry a non-empty description.

    Descriptions are the primary signal an AI agent uses to decide which
    argument to pass; a missing description silently degrades agent accuracy.
    """
    tool = _tool(client, "retrieve")
    props = tool["inputSchema"]["properties"]
    for name, prop in props.items():
        assert "description" in prop and prop["description"], (
            f"inputSchema.properties.{name} has no description"
        )


def test_retrieve_tool_advertises_output_schema(client: TestClient) -> None:
    """T-MCP13.1 — outputSchema declares the structuredContent.sources contract.

    Per MCP 2025-06-18, a tool that declares outputSchema MUST return
    conforming structuredContent; clients use the schema to parse the
    source list for UI display without re-parsing the text block.
    """
    tool = _tool(client, "retrieve")
    schema = tool["outputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["sources"]
    assert schema["additionalProperties"] is False
    items = schema["properties"]["sources"]["items"]
    assert items["additionalProperties"] is False
    assert set(items["properties"]) == {
        "document_id",
        "source_app",
        "source_id",
        "source_meta",
        "type",
        "source_title",
        "source_url",
        "mime_type",
        "excerpt",
        "score",
    }


def test_retrieve_tool_has_readonly_hint(client: TestClient) -> None:
    """Retrieve never writes data — readOnlyHint must be True.

    MCP hosts use this annotation to skip confirmation prompts for
    read-only tools (protocol 2025-03-26+). Clients on earlier versions
    ignore unknown fields, so this is backward compatible.
    """
    tool = _tool(client, "retrieve")
    assert tool.get("annotations", {}).get("readOnlyHint") is True
