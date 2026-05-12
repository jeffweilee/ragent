"""MCP server router — `POST /mcp/v1` JSON-RPC 2.0 (§3.8, B47).

Methods: initialize / notifications/initialized / tools/list / tools/call / ping.
Sole tool: `retrieve` (wraps POST /retrieve/v1).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft7Validator, ValidationError

from ragent import __version__ as _RAGENT_VERSION
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.pipelines.chat import (
    DEFAULT_TOP_K,
    build_es_filters,
    dedupe_by_document,
    doc_to_source_entry,
    run_retrieval,
)
from ragent.utility.env import int_env

logger = structlog.get_logger(__name__)

# JSON-RPC 2.0 standard error codes (spec §3.8.4).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_TOOL_EXECUTION_FAILED = -32001


class _McpToolError(Exception):
    """Raised by handlers to surface a JSON-RPC error envelope."""

    def __init__(self, code: int, error_code: str, message: str) -> None:
        self.code = code
        self.error_code = error_code
        super().__init__(message)


# MCP protocol pin (B47). Code constants, not env-driven — operators flipping
# these would silently break the contract advertised in `initialize`.
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_SERVER_NAME = "ragent"

# Defence-in-depth body-size cap. Production ingress (nginx / ALB) is the
# canonical bound; this is the second line. 256 KiB easily holds a fat
# JSON-RPC envelope (multi-kB query + nested filters) and rejects abuse.
# Env-tunable per the §4.6 inventory rule; mirrors `INGEST_INLINE_MAX_BYTES`.
_MAX_REQUEST_BYTES = int_env("MCP_REQUEST_MAX_BYTES", 256 * 1024)


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
                "default": DEFAULT_TOP_K,
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


_RETRIEVE_INPUT_VALIDATOR = Draft7Validator(_RETRIEVE_TOOL_SCHEMA["inputSchema"])


def _validate_retrieve_args(args: Any) -> None:
    """Validate `tools/call retrieve` arguments against the inputSchema.

    Raises `_McpToolError(-32602, MCP_TOOL_INPUT_INVALID, ...)` on the first
    failure. Draft7 is used because the schema authored in §3.8.3 uses
    `default`/`minimum`/`maximum`/`required` — all in scope of Draft7
    without needing 2020-12-only keywords.
    """
    try:
        _RETRIEVE_INPUT_VALIDATOR.validate(args)
    except ValidationError as exc:
        # `path` is the dotted location inside `args`; the root error has no
        # path so we substitute "arguments" for a readable message.
        location = ".".join(str(p) for p in exc.absolute_path) or "arguments"
        raise _McpToolError(
            _INVALID_PARAMS,
            HttpErrorCode.MCP_TOOL_INPUT_INVALID.value,
            f"{location}: {exc.message}",
        ) from exc


# Stateless handlers — composed before per-router state (the retrieval
# pipeline) is bound. T-MCP.8 adds the stateful `tools/call` handler as a
# closure inside `create_mcp_router` that captures the pipeline.
_STATELESS_METHODS: dict[str, Callable[[Any], Awaitable[dict[str, Any]]]] = {
    "ping": _handle_ping,
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
}


def create_mcp_router(retrieval_pipeline: Any) -> APIRouter:
    router = APIRouter(prefix="/mcp/v1")

    async def _handle_tools_call(params: Any) -> dict[str, Any]:
        # JSON-RPC 2.0 allows `params` to be omitted, an object, or an array.
        # We only accept the object form (named arguments); array/positional
        # surfaces as -32602 rather than a 500 AttributeError on `.get()`.
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            raise _McpToolError(
                _INVALID_PARAMS,
                HttpErrorCode.MCP_TOOL_INPUT_INVALID.value,
                "`params` must be an object (named arguments)",
            )
        name = params.get("name")
        if name != _RETRIEVE_TOOL_SCHEMA["name"]:
            raise _McpToolError(
                _INVALID_PARAMS,
                HttpErrorCode.MCP_TOOL_NOT_FOUND.value,
                f"unknown tool: {name!r}",
            )
        arguments = params.get("arguments") or {}
        _validate_retrieve_args(arguments)
        try:
            docs = await run_in_threadpool(
                run_retrieval,
                retrieval_pipeline,
                query=arguments["query"],
                filters=build_es_filters(arguments.get("source_app"), arguments.get("source_meta")),
                top_k=arguments.get("top_k", DEFAULT_TOP_K),
                min_score=arguments.get("min_score"),
            )
        except Exception as exc:
            logger.exception("mcp.tool.error", tool=name, error_type=type(exc).__name__)
            raise _McpToolError(
                _TOOL_EXECUTION_FAILED,
                HttpErrorCode.MCP_TOOL_EXECUTION_FAILED.value,
                str(exc) or "tool execution failed",
            ) from exc
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

    @router.post("")
    async def mcp_jsonrpc(request: Request) -> Response:
        # 413 Payload Too Large — transport-layer concern (NOT a JSON-RPC
        # error envelope) so the caller can distinguish from in-protocol
        # errors. Mirrors the §3.8.1 auth-failure rule.
        #
        # Pre-read on Content-Length where the client advertised one — avoids
        # buffering an attacker-sized body into memory before rejecting it.
        # Fall back to a post-read length check (covers clients that omit
        # Content-Length, e.g. chunked transfer).
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > _MAX_REQUEST_BYTES:
            return problem(
                413,
                HttpErrorCode.MCP_INVALID_REQUEST,
                f"request body exceeds {_MAX_REQUEST_BYTES} bytes",
            )
        raw = await request.body()
        if len(raw) > _MAX_REQUEST_BYTES:
            return problem(
                413,
                HttpErrorCode.MCP_INVALID_REQUEST,
                f"request body exceeds {_MAX_REQUEST_BYTES} bytes",
            )
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # UnicodeDecodeError fires when the body isn't valid UTF-8; per
            # JSON-RPC 2.0 §5, an unparseable body is a Parse error (-32700)
            # with id:null, NOT a transport 500.
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
            or not isinstance(envelope.get("method"), str)
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

        try:
            result = await handler(envelope.get("params"))
        except _McpToolError as exc:
            return JSONResponse(
                _jsonrpc_error(
                    req_id,
                    exc.code,
                    str(exc),
                    data={"error_code": exc.error_code},
                )
            )
        return JSONResponse(_jsonrpc_result(req_id, result))

    return router
