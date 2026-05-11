"""MCP server router (§3.8, B47).

P1 (T6.1): `POST /mcp/v1/tools/rag` returns 501 — removed in T-MCP.12.
P2.5: `POST /mcp/v1` JSON-RPC 2.0 server exposing the `retrieve` tool.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem

# JSON-RPC 2.0 standard error codes (spec §3.8.4).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601


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


# Dispatch table. T-MCP.4 / T-MCP.6 / T-MCP.8 extend with `initialize`,
# `tools/list`, `tools/call`. Handlers are coroutines so the future
# `tools/call` handler (T-MCP.8) can `await` the async retrieval pipeline
# without a sync/async branch in the dispatcher.
_METHODS: dict[str, Callable[[Any], Awaitable[dict[str, Any]]]] = {
    "ping": _handle_ping,
}


def create_mcp_router() -> APIRouter:
    router = APIRouter(prefix="/mcp/v1")

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

        handler = _METHODS.get(method)
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
