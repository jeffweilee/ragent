"""MCP server router (§3.8, B47).

P1 (T6.1): `POST /mcp/v1/tools/rag` returns 501 — removed in T-MCP.12.
P2.5: `POST /mcp/v1` JSON-RPC 2.0 server exposing the `retrieve` tool.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from ragent import __version__ as _RAGENT_VERSION
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.pipelines.chat import (
    build_es_filters,
    dedupe_by_document,
    doc_to_source_entry,
    run_retrieval,
)

# JSON-RPC 2.0 standard error codes (spec §3.8.4).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601

# MCP protocol pin (B47). Code constants, not env-driven — operators flipping
# these would silently break the contract advertised in `initialize`.
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_SERVER_NAME = "ragent"


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


async def _handle_ping(_params: Any) -> dict[str, Any]:
    return {}


async def _handle_initialize(_params: Any) -> dict[str, Any]:
    return {
        "protocolVersion": _MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": _MCP_SERVER_NAME, "version": _RAGENT_VERSION},
    }


# Tool schema is the single source of truth: `tools/list` advertises it,
# `tools/call` (T-MCP.10) validates arguments against it. Mirrors
# §3.8.3 verbatim.
_RETRIEVE_TOOL_SCHEMA: dict[str, Any] = {
    "name": "retrieve",
    "description": (
        "Retrieve relevant document chunks from the ragent corpus using "
        "hybrid vector+BM25 search with optional reranking. Returns ranked "
        "chunks (no LLM synthesis)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Natural-language query.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 20,
            },
            "source_app": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Optional ES term filter.",
            },
            "source_meta": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
                "description": "Optional ES term filter.",
            },
            "min_score": {
                "type": "number",
                "minimum": 0,
                "description": "Optional post-pipeline score floor.",
            },
            "dedupe": {
                "type": "boolean",
                "default": False,
                "description": "Keep one chunk per document_id.",
            },
        },
        "required": ["query"],
    },
}


async def _handle_tools_list(_params: Any) -> dict[str, Any]:
    return {"tools": [_RETRIEVE_TOOL_SCHEMA]}


# Stateless handlers — composed before per-router state (the retrieval
# pipeline) is bound. T-MCP.8 adds the stateful `tools/call` handler as a
# closure inside `create_mcp_router` that captures the pipeline.
_STATELESS_METHODS: dict[str, Callable[[Any], Awaitable[dict[str, Any]]]] = {
    "ping": _handle_ping,
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
}


def create_mcp_router(retrieval_pipeline: Any = None) -> APIRouter:
    router = APIRouter(prefix="/mcp/v1")

    async def _handle_tools_call(params: Any) -> dict[str, Any]:
        # Argument extraction (T-MCP.10 will add schema validation +
        # MCP_TOOL_NOT_FOUND / MCP_TOOL_INPUT_INVALID error mapping).
        params = params or {}
        arguments = params.get("arguments") or {}
        docs = await run_in_threadpool(
            run_retrieval,
            retrieval_pipeline,
            query=arguments["query"],
            filters=build_es_filters(arguments.get("source_app"), arguments.get("source_meta")),
            top_k=arguments.get("top_k", 20),
            min_score=arguments.get("min_score"),
        )
        if arguments.get("dedupe"):
            docs = dedupe_by_document(docs)
        payload = {"chunks": [doc_to_source_entry(d) for d in docs]}
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False,
        }

    methods: dict[str, Callable[[Any], Awaitable[dict[str, Any]]]] = {
        **_STATELESS_METHODS,
        "tools/call": _handle_tools_call,
    }

    @router.post("/tools/rag")
    async def rag_tool() -> Response:
        return problem(
            501,
            HttpErrorCode.MCP_NOT_IMPLEMENTED,
            "MCP RAG tool not implemented in Phase 1",
        )

    @router.post("")
    async def mcp_jsonrpc(request: Request) -> Response:
        # Pre-`json.loads` body-size cap not enforced here — production
        # ingress (nginx / ALB) is the canonical bound; a defence-in-depth
        # router-level cap is tracked for a follow-up commit pending spec
        # §3.8.1 pinning the limit + matching `MCP_REQUEST_MAX_BYTES` env.
        raw = await request.body()
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse(
                _jsonrpc_error(
                    None,
                    _PARSE_ERROR,
                    "Parse error",
                    data={"error_code": HttpErrorCode.MCP_PARSE_ERROR.value},
                )
            )

        if (
            not isinstance(envelope, dict)
            or envelope.get("jsonrpc") != "2.0"
            or "method" not in envelope
        ):
            req_id = envelope.get("id") if isinstance(envelope, dict) else None
            return JSONResponse(
                _jsonrpc_error(
                    req_id,
                    _INVALID_REQUEST,
                    "Invalid Request",
                    data={"error_code": HttpErrorCode.MCP_INVALID_REQUEST.value},
                )
            )

        method = envelope["method"]
        # JSON-RPC 2.0 §4.1: a notification is a request WITHOUT the `id`
        # member. `id: null` is a valid request, not a notification.
        is_notification = "id" not in envelope
        req_id = envelope.get("id")

        if is_notification:
            # No JSON-RPC response object for notifications — even when the
            # method name is unrecognised. HTTP 204 is the streamable-HTTP
            # transport mapping.
            return Response(status_code=204)

        handler = methods.get(method)
        if handler is None:
            return JSONResponse(
                _jsonrpc_error(
                    req_id,
                    _METHOD_NOT_FOUND,
                    f"Method not found: {method}",
                    data={"error_code": HttpErrorCode.MCP_METHOD_NOT_FOUND.value},
                )
            )

        result = await handler(envelope.get("params"))
        return JSONResponse(_jsonrpc_result(req_id, result))

    return router
