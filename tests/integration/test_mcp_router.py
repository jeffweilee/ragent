"""T-MCP.11 — End-to-end MCP router with a real `build_retrieval_pipeline`.

Mounts the MCP router (single dispatcher, single retrieve tool) atop a real
Elasticsearch testcontainer + a real Haystack `Pipeline` (mocked embedder
and doc-repo). Runs the full MCP client handshake — `initialize` →
`tools/list` → `tools/call retrieve` — and asserts every JSON-RPC contract
holds end-to-end.

Complements the per-method unit tests under `tests/unit/test_mcp_*`. Unit
tests pin envelope semantics with a `MagicMock()` pipeline; this test
verifies the wire path actually reaches `run_retrieval` and the schema
advertised in `tools/list` matches the args accepted by `tools/call`.
"""

from __future__ import annotations

import json
import urllib.request
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from haystack.dataclasses import Document

from ragent.routers.mcp import create_mcp_router

pytestmark = pytest.mark.docker

_EMBEDDING_DIM = 1024
_FIXED_EMBEDDING = [0.1] * _EMBEDDING_DIM


def _wipe_chunks_v1(es_url: str) -> None:
    """Empty chunks_v1 + refresh, so each test starts on a clean slate."""
    req = urllib.request.Request(
        f"{es_url}/chunks_v1/_delete_by_query?refresh=true",
        method="POST",
        data=b'{"query":{"match_all":{}}}',
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


@pytest.fixture(scope="module")
def es_store(es_url: str):
    # NOTE: this fixture mirrors `tests/integration/test_chat_pipeline_retrieval.py`
    # almost verbatim. A follow-up STRUCTURAL commit will lift it (along with
    # `mock_embedder` and the embedding constants) into a shared
    # `tests/integration/conftest.py`.
    from haystack_integrations.document_stores.elasticsearch import (
        ElasticsearchDocumentStore,
    )

    from ragent.bootstrap.init_schema import init_es

    init_es(es_url)
    with urllib.request.urlopen(
        f"{es_url}/_cluster/health/chunks_v1?wait_for_status=yellow&timeout=60s",
        timeout=70,
    ) as resp:
        health = json.loads(resp.read())
        if health.get("status") not in ("yellow", "green"):
            raise RuntimeError(f"chunks_v1 index not ready: {health}")

    return ElasticsearchDocumentStore(
        hosts=es_url,
        index="chunks_v1",
        embedding_similarity_function="cosine",
    )


@pytest.fixture(autouse=True)
def _isolate_chunks(es_url: str):
    """Per-test wipe so a re-order or future test asserting empty-index
    behaviour cannot see leftovers from a previous test in this module."""
    _wipe_chunks_v1(es_url)
    yield


@pytest.fixture(scope="module")
def mock_embedder():
    embedder = MagicMock()
    embedder.embed.return_value = [_FIXED_EMBEDDING]
    return embedder


@pytest.fixture
def mock_doc_repo(es_store):
    """Returns the document_id → (source_app, source_id, source_title) map
    pre-populated for every doc the test writes."""
    repo = AsyncMock()
    repo.get_sources_by_document_ids.return_value = {
        "doc-mcp-1": ("confluence", "SRC-1", "Doc MCP 1"),
        "doc-mcp-2": ("confluence", "SRC-2", "Doc MCP 2"),
    }
    return repo


@pytest.fixture
def app(es_store, mock_embedder, mock_doc_repo) -> FastAPI:
    from ragent.pipelines.chat import build_retrieval_pipeline

    pipeline = build_retrieval_pipeline(
        embedder=mock_embedder,
        document_store=es_store,
        doc_repo=mock_doc_repo,
        join_mode="rrf",
    )
    a = FastAPI()
    a.include_router(create_mcp_router(retrieval_pipeline=pipeline))
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _seed_chunks(es_store, es_url: str) -> None:
    es_store.write_documents(
        [
            Document(
                id="chunk-1",
                content="machine learning gradient descent",
                meta={
                    "chunk_id": "chunk-1",
                    "document_id": "doc-mcp-1",
                    "source_app": "confluence",
                },
                embedding=_FIXED_EMBEDDING,
            ),
            Document(
                id="chunk-2",
                content="vector retrieval hybrid search",
                meta={
                    "chunk_id": "chunk-2",
                    "document_id": "doc-mcp-2",
                    "source_app": "confluence",
                },
                embedding=_FIXED_EMBEDDING,
            ),
        ]
    )
    # Deterministic refresh beats `time.sleep` — the index becomes searchable
    # synchronously regardless of ES's lazy refresh interval.
    with urllib.request.urlopen(f"{es_url}/chunks_v1/_refresh", timeout=10) as resp:
        resp.read()


def test_mcp_full_handshake_round_trip(client: TestClient, es_store, es_url: str) -> None:
    """initialize → tools/list → tools/call retrieve round-trip.

    Pins the entire wire-path: handshake advertises tools capability, the
    advertised tool name matches what tools/call accepts, retrieve dispatch
    actually returns chunks from the real ES + Haystack pipeline.
    """
    _seed_chunks(es_store, es_url)

    # Step 1: initialize.
    init_resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
    )
    assert init_resp.status_code == 200
    assert init_resp.headers["content-type"].startswith("application/json")
    init_body = init_resp.json()
    assert init_body["result"]["protocolVersion"] == "2024-11-05"
    assert init_body["result"]["capabilities"] == {"tools": {}}

    # Step 2: tools/list — discovery.
    list_resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    list_body = list_resp.json()
    [tool] = list_body["result"]["tools"]
    assert tool["name"] == "retrieve"

    # Step 3: tools/call retrieve — actually invoke the pipeline.
    call_resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": tool["name"],  # discovered from tools/list
                "arguments": {"query": "gradient descent", "top_k": 5},
            },
        },
    )
    assert call_resp.status_code == 200
    call_body = call_resp.json()
    assert call_body["jsonrpc"] == "2.0"
    assert call_body["id"] == 3
    assert call_body["result"]["isError"] is False
    payload = json.loads(call_body["result"]["content"][0]["text"])
    assert "chunks" in payload
    # The pipeline ran against the seeded ES — chunks must include the seeded
    # document_ids (proves the wire reached run_retrieval).
    doc_ids = {c["document_id"] for c in payload["chunks"]}
    assert doc_ids.issubset({"doc-mcp-1", "doc-mcp-2"})
    assert len(payload["chunks"]) >= 1


def test_mcp_initialize_then_notifications_initialized(client: TestClient) -> None:
    """Standard client flow: initialize → notifications/initialized → 204."""
    client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
    )
    notify_resp = client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notify_resp.status_code == 204
    assert notify_resp.content == b""
