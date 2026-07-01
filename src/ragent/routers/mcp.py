"""MCP server router — `POST /mcp/v1` JSON-RPC 2.0 (§3.8, B47).

Methods: initialize / notifications/initialized / tools/list / tools/call / ping.
Sole tool: `retrieve` (wraps POST /retrieve/v1).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import structlog
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft7Validator, ValidationError
from mcp.types import Tool

from ragent import __version__ as _RAGENT_VERSION
from ragent.auth.deps import get_user_id
from ragent.errors.codes import HttpErrorCode
from ragent.errors.problem import problem
from ragent.pipelines.retrieve import (
    DEFAULT_TOP_K,
    EXCERPT_MAX_CHARS_DEFAULT,
    build_es_filters,
    dedupe_by_document,
    doc_to_source_entry,
    run_retrieval,
)
from ragent.routers.mcp_tools.create_skill import CREATE_SKILL_TOOL
from ragent.routers.mcp_tools.retrieve import RETRIEVE_TOOL
from ragent.routers.mcp_tools.skill_tools import (
    DELETE_SKILL_TOOL,
    GET_SKILL_TOOL,
    LIST_SKILLS_TOOL,
    UPDATE_SKILL_TOOL,
)
from ragent.services.skill_service import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillReadOnlyError,
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


# MCP protocol revisions (B47/B63). Code constants, not env-driven — operators
# flipping these would silently break the contract advertised in `initialize`.
# Newest first; 2025-06-18 is the first revision with tool outputSchema /
# structuredContent (older clients ignore those additive result fields).
_SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
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


async def _handle_initialize(params: Any) -> dict[str, Any]:
    # MCP version negotiation: echo the requested revision when supported,
    # otherwise answer with the latest we speak (mirrors the official SDK).
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    version = (
        requested
        if requested in _SUPPORTED_PROTOCOL_VERSIONS
        else (_SUPPORTED_PROTOCOL_VERSIONS[0])
    )
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": _MCP_SERVER_NAME, "version": _RAGENT_VERSION},
    }


def _tools_payload(tools: list[Tool]) -> list[dict[str, Any]]:
    # tools/list is read-only; the set is fixed per router instance (depends on
    # whether skill_service is wired), so it is built once in create_mcp_router.
    return [t.model_dump(exclude_none=True) for t in tools]


# Uses the same inputSchema already advertised by tools/list — schema and
# validation can never drift apart.
_RETRIEVE_INPUT_VALIDATOR = Draft7Validator(RETRIEVE_TOOL.inputSchema)
_CREATE_SKILL_INPUT_VALIDATOR = Draft7Validator(CREATE_SKILL_TOOL.inputSchema)
_LIST_SKILLS_INPUT_VALIDATOR = Draft7Validator(LIST_SKILLS_TOOL.inputSchema)
_GET_SKILL_INPUT_VALIDATOR = Draft7Validator(GET_SKILL_TOOL.inputSchema)
_UPDATE_SKILL_INPUT_VALIDATOR = Draft7Validator(UPDATE_SKILL_TOOL.inputSchema)
_DELETE_SKILL_INPUT_VALIDATOR = Draft7Validator(DELETE_SKILL_TOOL.inputSchema)

# SkillService domain errors → JSON-RPC error codes (all are caller errors).
_SKILL_ERROR_CODES: dict[type[Exception], HttpErrorCode] = {
    SkillNotFoundError: HttpErrorCode.SKILL_NOT_FOUND,
    SkillNameConflictError: HttpErrorCode.SKILL_NAME_CONFLICT,
    SkillReadOnlyError: HttpErrorCode.SKILL_READONLY,
}


def _require_mcp_user(user_id: str | None) -> None:
    """Fail closed when no identity reached the MCP endpoint — a skill is never
    created/read/mutated under an unknown or attacker-chosen owner."""
    if not user_id:
        raise _McpToolError(
            _INVALID_PARAMS,
            HttpErrorCode.MISSING_USER_ID.value,
            "user identity required for skill tools",
        )


def _raise_skill_tool_error(tool: str, exc: Exception) -> NoReturn:
    """Map a SkillService failure to a JSON-RPC error envelope.

    Typed domain errors (not-found / conflict / read-only) are caller errors
    (-32602); anything unexpected (DB outage, write error) is
    MCP_TOOL_EXECUTION_FAILED (-32001) — never an HTTP 500. Mirrors the retrieve
    tool's wrapping. A `_McpToolError` raised by input validation is re-raised
    unchanged (defence in depth; validation runs before the wrapped call).
    """
    if isinstance(exc, _McpToolError):
        raise exc
    code = _SKILL_ERROR_CODES.get(type(exc))
    if code is not None:
        raise _McpToolError(_INVALID_PARAMS, code.value, str(exc)) from exc
    logger.exception("mcp.tool.error", tool=tool, error_type=type(exc).__name__)
    raise _McpToolError(
        _TOOL_EXECUTION_FAILED,
        HttpErrorCode.MCP_TOOL_EXECUTION_FAILED.value,
        str(exc) or "tool execution failed",
    ) from exc


def _skill_brief(resp: Any) -> dict[str, Any]:
    return {
        "skill_id": resp.skill_id,
        "name": resp.name,
        "description": resp.description,
        "enabled": resp.enabled,
        "readonly": resp.readonly,
    }


def _skill_full(resp: Any) -> dict[str, Any]:
    return {
        **_skill_brief(resp),
        "instructions": resp.instructions,
        "created_at": resp.created_at,
        "updated_at": resp.updated_at,
    }


def _validate_against(validator: Draft7Validator, args: Any) -> None:
    """Validate `tools/call` arguments against a tool's inputSchema.

    Raises `_McpToolError(-32602, MCP_TOOL_INPUT_INVALID, ...)` on the first
    failure. `additionalProperties:false` on each schema means stray fields
    (e.g. a spoofed ``user_id``) are rejected here, not silently ignored. Draft7
    is used because the schemas use `default`/`minimum`/`maximum`/`required`.
    """
    try:
        validator.validate(args)
    except ValidationError as exc:
        # `path` is the dotted location inside `args`; the root error has no
        # path so we substitute "arguments" for a readable message.
        location = ".".join(str(p) for p in exc.absolute_path) or "arguments"
        raise _McpToolError(
            _INVALID_PARAMS,
            HttpErrorCode.MCP_TOOL_INPUT_INVALID.value,
            f"{location}: {exc.message}",
        ) from exc


def _validate_retrieve_args(args: Any) -> None:
    _validate_against(_RETRIEVE_INPUT_VALIDATOR, args)


# Corpus text containing literal <context>/</context> tags must not close the
# wrapper that hosts use to isolate tool context (PR #171 codex review).
_CONTEXT_TAG_RE = re.compile(r"<(/?context)>", re.IGNORECASE)


def _neutralize_context_tags(value: str) -> str:
    return _CONTEXT_TAG_RE.sub(r"&lt;\1&gt;", value)


def _header_field(value: str | None) -> str:
    """Sanitise a metadata value for a single markdown line: CR/LF stripped,
    embedded <context> tags neutralised."""
    return _neutralize_context_tags((value or "").replace("\n", " ").replace("\r", ""))


def _md_cell(value: str | None) -> str:
    """Sanitise a value for a markdown table cell: single line, `|` escaped
    so a malicious title cannot break the table or inject rows."""
    return _header_field(value).replace("|", "\\|")


def _safe_link_url(value: str | None) -> str:
    """Return a linkifiable URL or "" (render plain title instead).

    Only http(s) destinations are linkified — a crafted javascript: URL must
    not become a clickable link in user-presentable markdown. Characters that
    terminate a markdown link destination or split a table cell are
    percent-encoded; the raw URL stays in structuredContent.
    """
    # Sanitise before encoding — a CR/LF must become %20, not a raw space.
    url = _header_field(value).strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    for char, encoded in (("(", "%28"), (")", "%29"), (" ", "%20"), ("|", "%7C")):
        url = url.replace(char, encoded)
    return url


def _render_context_markdown(entries: list[dict]) -> str:
    """Render retrieve entries as a <context>-wrapped markdown digest.

    Layout: a user-presentable citation table (#, 資料來源, 來源系統 — no
    internal fields like document_id/score, those live in structuredContent),
    then one `### [N]` blockquoted excerpt section per source for LLM
    grounding. No natural-language wording, so calling LLMs treat the block
    as injected context data rather than prose to transcribe.
    """
    if not entries:
        return "<context>\n</context>"
    rows = ["| # | 資料來源 | 來源系統 |", "|---|---------|---------|"]
    excerpt_blocks = []
    for i, entry in enumerate(entries, start=1):
        # Pipe-escaping is a table-cell concern only — headings keep the raw `|`.
        title = _header_field(entry.get("source_title")) or "(未命名)"
        cell_title = title.replace("|", "\\|")
        url = _safe_link_url(entry.get("source_url"))
        link = f"[{cell_title}]({url})" if url else cell_title
        rows.append(f"| {i} | {link} | {_md_cell(entry.get('source_app'))} |")
        excerpt = _neutralize_context_tags(entry.get("excerpt") or "")
        quoted = "\n".join(f"> {line}" for line in excerpt.splitlines() or [""])
        excerpt_blocks.append(f"### [{i}] {title}\n{quoted}")
    body = "\n".join(rows) + "\n\n" + "\n\n".join(excerpt_blocks)
    return f"<context>\n{body}\n</context>"


# Stateless handlers — no per-router state needed. `tools/list` and `tools/call`
# are built inside create_mcp_router because the tool set depends on whether
# skill_service is wired, and `create_skill` needs the per-request caller id.
_StatelessHandler = Callable[[Any], Awaitable[dict[str, Any]]]
_STATELESS_METHODS: dict[str, _StatelessHandler] = {
    "ping": _handle_ping,
    "initialize": _handle_initialize,
}

# A tool handler receives (validated-shape params arguments, caller user_id).
_ToolHandler = Callable[[dict[str, Any], "str | None"], Awaitable[dict[str, Any]]]


def create_mcp_router(
    retrieval_pipeline: Any,
    *,
    skill_service: Any | None = None,
    excerpt_max_chars: int = EXCERPT_MAX_CHARS_DEFAULT,
) -> APIRouter:
    router = APIRouter(prefix="/mcp/v1")

    async def _run_retrieve(arguments: dict[str, Any], _user_id: str | None) -> dict[str, Any]:
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
            logger.exception("mcp.tool.error", tool="retrieve", error_type=type(exc).__name__)
            raise _McpToolError(
                _TOOL_EXECUTION_FAILED,
                HttpErrorCode.MCP_TOOL_EXECUTION_FAILED.value,
                str(exc) or "tool execution failed",
            ) from exc
        if arguments.get("dedupe"):
            docs = dedupe_by_document(docs)
        entries = [doc_to_source_entry(d, max_chars=excerpt_max_chars) for d in docs]
        # Dual channel: sources JSON for the UI, markdown digest for the LLM.
        return {
            "content": [{"type": "text", "text": _render_context_markdown(entries)}],
            "structuredContent": {"sources": entries},
            "isError": False,
        }

    # Owner is the authenticated caller — NEVER a tool argument. Each skill
    # handler fails closed on missing identity, validates against its schema
    # (additionalProperties:false rejects a spoofed user_id), then maps
    # SkillService errors onto JSON-RPC envelopes via _raise_skill_tool_error.

    async def _run_create_skill(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        _require_mcp_user(user_id)
        _validate_against(_CREATE_SKILL_INPUT_VALIDATOR, arguments)
        try:
            resp = await skill_service.create(
                user_id=user_id,
                name=arguments["name"],
                description=arguments.get("description", ""),
                instructions=arguments["instructions"],
                enabled=arguments.get("enabled", True),
            )
        except Exception as exc:
            _raise_skill_tool_error("create_skill", exc)
        text = (
            f"Created skill '{resp.name}' (skill_id={resp.skill_id}). "
            "The user can select it for a future chat turn."
        )
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"skill": _skill_brief(resp)},
            "isError": False,
        }

    async def _run_list_skills(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        _require_mcp_user(user_id)
        _validate_against(_LIST_SKILLS_INPUT_VALIDATOR, arguments)
        try:
            resps = await skill_service.list_for_user(user_id=user_id)
        except Exception as exc:
            _raise_skill_tool_error("list_skills", exc)
        briefs = [_skill_brief(r) for r in resps]
        return {
            "content": [{"type": "text", "text": f"{len(briefs)} skill(s)."}],
            "structuredContent": {"skills": briefs},
            "isError": False,
        }

    async def _run_get_skill(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        _require_mcp_user(user_id)
        _validate_against(_GET_SKILL_INPUT_VALIDATOR, arguments)
        try:
            resp = await skill_service.get(user_id=user_id, skill_id=arguments["skill_id"])
        except Exception as exc:
            _raise_skill_tool_error("get_skill", exc)
        return {
            "content": [{"type": "text", "text": f"Skill '{resp.name}'."}],
            "structuredContent": {"skill": _skill_full(resp)},
            "isError": False,
        }

    async def _run_update_skill(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        _require_mcp_user(user_id)
        _validate_against(_UPDATE_SKILL_INPUT_VALIDATOR, arguments)
        try:
            # A full replace: the schema requires every write field, so we read
            # them directly — no `.get` default that could silently overwrite.
            resp = await skill_service.update(
                user_id=user_id,
                skill_id=arguments["skill_id"],
                name=arguments["name"],
                description=arguments["description"],
                instructions=arguments["instructions"],
                enabled=arguments["enabled"],
            )
        except Exception as exc:
            _raise_skill_tool_error("update_skill", exc)
        return {
            "content": [
                {"type": "text", "text": f"Updated skill '{resp.name}' (skill_id={resp.skill_id})."}
            ],
            "structuredContent": {"skill": _skill_full(resp)},
            "isError": False,
        }

    async def _run_delete_skill(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        _require_mcp_user(user_id)
        _validate_against(_DELETE_SKILL_INPUT_VALIDATOR, arguments)
        skill_id = arguments["skill_id"]
        try:
            await skill_service.delete(user_id=user_id, skill_id=skill_id)
        except Exception as exc:
            _raise_skill_tool_error("delete_skill", exc)
        return {
            "content": [{"type": "text", "text": f"Deleted skill {skill_id}."}],
            "structuredContent": {"skill_id": skill_id, "deleted": True},
            "isError": False,
        }

    tools: list[Tool] = [RETRIEVE_TOOL]
    tool_handlers: dict[str, _ToolHandler] = {"retrieve": _run_retrieve}
    if skill_service is not None:
        tools.extend(
            [
                CREATE_SKILL_TOOL,
                LIST_SKILLS_TOOL,
                GET_SKILL_TOOL,
                UPDATE_SKILL_TOOL,
                DELETE_SKILL_TOOL,
            ]
        )
        tool_handlers.update(
            {
                "create_skill": _run_create_skill,
                "list_skills": _run_list_skills,
                "get_skill": _run_get_skill,
                "update_skill": _run_update_skill,
                "delete_skill": _run_delete_skill,
            }
        )
    tools_list_payload = _tools_payload(tools)

    async def _handle_tools_list(_params: Any) -> dict[str, Any]:
        return {"tools": tools_list_payload}

    async def _handle_tools_call(params: Any, user_id: str | None) -> dict[str, Any]:
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
        handler = tool_handlers.get(params.get("name"))
        if handler is None:
            raise _McpToolError(
                _INVALID_PARAMS,
                HttpErrorCode.MCP_TOOL_NOT_FOUND.value,
                f"unknown tool: {params.get('name')!r}",
            )
        # Distinguish "omitted/null" (→ {}) from a falsy non-object like [] / "" /
        # False: `or {}` would silently coerce the latter to {} and bypass the
        # type check, accepting an invalid arguments shape.
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            raise _McpToolError(
                _INVALID_PARAMS,
                HttpErrorCode.MCP_TOOL_INPUT_INVALID.value,
                "`arguments` must be an object",
            )
        return await handler(arguments, user_id)

    methods: dict[str, _StatelessHandler] = {
        **_STATELESS_METHODS,
        "tools/list": _handle_tools_list,
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

        # tools/call is dispatched separately because it needs the per-request
        # caller identity (owner of any created skill); the other methods do not.
        is_tools_call = method == "tools/call"
        handler = methods.get(method)
        if handler is None and not is_tools_call:
            return JSONResponse(
                _jsonrpc_error(
                    req_id,
                    _METHOD_NOT_FOUND,
                    f"Method not found: {method}",
                    data={"error_code": HttpErrorCode.MCP_METHOD_NOT_FOUND.value},
                )
            )

        try:
            if is_tools_call:
                user_id = await get_user_id(request)
                result = await _handle_tools_call(envelope.get("params"), user_id)
            else:
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
