"""T-MCP.7 — Pin `tools/call retrieve` happy path (S60).

Spec §3.8.3 (tool input schema), §3.8.5 S60 (result shape mirrors §3.4.4
RetrieveResponse, JSON-stringified into `content[0].text`).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router


def _make_doc(doc_id: str, source_app: str = "confluence") -> SimpleNamespace:
    return SimpleNamespace(
        meta={
            "document_id": doc_id,
            "source_app": source_app,
            "source_id": f"SRC-{doc_id}",
            "source_meta": "engineering",
            "source_title": f"Title {doc_id}",
            "source_url": f"https://wiki/{doc_id}",
            "mime_type": "text/plain",
            "raw_content": f"raw {doc_id}",
        },
        content=f"chunk text for {doc_id}",
        score=0.9,
    )


@pytest.fixture
def app() -> FastAPI:
    pipeline = MagicMock()
    a = FastAPI()
    a.include_router(create_mcp_router(retrieval_pipeline=pipeline))
    return a


@pytest.fixture
def client_factory(app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    def _factory(docs: list[SimpleNamespace]) -> TestClient:
        monkeypatch.setattr(
            "ragent.routers.mcp.run_retrieval",
            lambda *_a, **_kw: list(docs),
        )
        return TestClient(app)

    return _factory


def test_tools_call_retrieve_returns_text_content_array(client_factory) -> None:
    """S60 — content[0].type == "text" with JSON-stringified body."""
    docs = [_make_doc("d1"), _make_doc("d2"), _make_doc("d3")]
    client = client_factory(docs)
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "retrieve", "arguments": {"query": "q", "top_k": 3}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert result["isError"] is False
    assert isinstance(result["content"], list)
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"


def test_tools_call_retrieve_payload_is_json_with_chunks(client_factory) -> None:
    """S60 — content[0].text parses as JSON {chunks: list}; length matches docs."""
    docs = [_make_doc("d1"), _make_doc("d2"), _make_doc("d3")]
    client = client_factory(docs)
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "retrieve", "arguments": {"query": "q", "top_k": 3}},
        },
    )
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert set(payload.keys()) == {"chunks"}
    assert len(payload["chunks"]) == 3
    # Each chunk mirrors RetrieveResponse.ChunkEntry: document_id, source_app,
    # source_id, source_meta, type, source_title, source_url, mime_type,
    # excerpt, score.
    sample = payload["chunks"][0]
    assert sample["document_id"] == "d1"
    assert sample["source_app"] == "confluence"
    assert sample["score"] == 0.9
    assert "excerpt" in sample


def test_tools_call_retrieve_passes_arguments_to_pipeline(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-MCP.8 dispatch must forward query / top_k / filter args to run_retrieval."""
    captured: dict = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("ragent.routers.mcp.run_retrieval", _capture)
    client = TestClient(app)
    client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "retrieve",
                "arguments": {
                    "query": "needle",
                    "top_k": 5,
                    "source_app": "confluence",
                    "source_meta": "engineering",
                    "min_score": 0.3,
                },
            },
        },
    )
    assert captured["query"] == "needle"
    assert captured["top_k"] == 5
    assert captured["min_score"] == 0.3
    # filters are built via build_es_filters; assert shape contains the filters.
    assert captured["filters"] is not None


def test_tools_call_retrieve_empty_result(client_factory) -> None:
    """Empty retrieval returns isError:false with chunks:[]."""
    client = client_factory([])
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "retrieve", "arguments": {"query": "q"}},
        },
    )
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload == {"chunks": []}
    assert resp.json()["result"]["isError"] is False
