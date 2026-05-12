"""Centralized error-code inventory (00_rule.md §API Error Honesty).

Three enums by SURFACE — they have different audiences and contracts:

- ``HttpErrorCode`` — surfaces in the RFC 9457 problem-details body of
  4xx / 5xx HTTP responses. The public contract for downstream API
  callers (rate-limit, validation, upstream-failure routing).
- ``TaskErrorCode`` — written to ``documents.error_code`` by the worker
  and reconciler; surfaces via ``GET /ingest/{id}`` for clients that
  poll async ingest status.
- ``ProbeErrorCode`` — emitted in ``/readyz`` per-component JSON for
  SRE monitoring; never reaches an API caller.

Splitting by surface (not by domain) is intentional: a value moving
across surfaces is a behaviour change worth a code review.

Each enum is a ``StrEnum``: ``HttpErrorCode.LLM_ERROR == "LLM_ERROR"``
is True, so existing string-literal call sites and JSON serialization
keep working without churn while new code can import the enum member
for typo-safety.
"""

from __future__ import annotations

from enum import StrEnum


class HttpErrorCode(StrEnum):
    """Codes that land in problem-details responses (4xx / 5xx)."""

    # Generic — global handler fallback when an exception has no error_code.
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # Upstream service failures (502 / 504).
    # Defaults on the UpstreamServiceError / UpstreamTimeoutError base
    # classes; production clients always pass a service-specific code below.
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    EMBEDDER_ERROR = "EMBEDDER_ERROR"
    EMBEDDER_TIMEOUT = "EMBEDDER_TIMEOUT"
    LLM_ERROR = "LLM_ERROR"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    RERANK_ERROR = "RERANK_ERROR"
    RERANK_TIMEOUT = "RERANK_TIMEOUT"

    # Ingest router validation (4xx).
    INGEST_MIME_UNSUPPORTED = "INGEST_MIME_UNSUPPORTED"  # 415
    INGEST_FILE_TOO_LARGE = "INGEST_FILE_TOO_LARGE"  # 413
    INGEST_MINIO_SITE_UNKNOWN = "INGEST_MINIO_SITE_UNKNOWN"  # 422
    INGEST_OBJECT_NOT_FOUND = "INGEST_OBJECT_NOT_FOUND"  # 422
    INGEST_VALIDATION = "INGEST_VALIDATION"  # 422
    INGEST_NOT_FOUND = "INGEST_NOT_FOUND"  # 404 — document_id not in DB

    # Identity / middleware (422).
    MISSING_USER_ID = "MISSING_USER_ID"

    # Chat (429).
    CHAT_RATE_LIMITED = "CHAT_RATE_LIMITED"

    # MCP JSON-RPC 2.0 server (§3.8, B47). Surfaces as `data.error_code`
    # inside JSON-RPC error envelopes (NOT as problem+json), but lives in
    # this enum so the API-emitted code inventory remains single-source.
    MCP_PARSE_ERROR = "MCP_PARSE_ERROR"  # -32700
    MCP_INVALID_REQUEST = "MCP_INVALID_REQUEST"  # -32600
    MCP_METHOD_NOT_FOUND = "MCP_METHOD_NOT_FOUND"  # -32601
    MCP_TOOL_NOT_FOUND = "MCP_TOOL_NOT_FOUND"  # -32602 (tools/call unknown name)
    MCP_TOOL_INPUT_INVALID = "MCP_TOOL_INPUT_INVALID"  # -32602 (inputSchema violation)
    MCP_TOOL_EXECUTION_FAILED = "MCP_TOOL_EXECUTION_FAILED"  # -32001 (handler raised)


class TaskErrorCode(StrEnum):
    """Codes persisted to ``documents.error_code`` by async ingest paths."""

    # Per-step failures wrapped by pipelines/observability::wrap_component_run.
    PIPELINE_UNROUTABLE = "PIPELINE_UNROUTABLE"  # mime → splitter has no route
    CHUNK_BUDGET_EXCEEDED = "CHUNK_BUDGET_EXCEEDED"  # CHUNK_MAX_PIECES_PER_ATOM
    ES_WRITE_ERROR = "ES_WRITE_ERROR"  # DocumentWriter raised
    EMBEDDER_ERROR = "EMBEDDER_ERROR"  # embedder client raised inside step

    # Step fallback when wrap_component_run catches an exception that wasn't
    # pre-tagged with an error_code. Honest name (former "PIPELINE_TIMEOUT"
    # was misleading: it fires for any unexpected error, not only timeouts).
    PIPELINE_UNEXPECTED_ERROR = "PIPELINE_UNEXPECTED_ERROR"

    # Worker-level outcomes.
    PIPELINE_TIMEOUT_AGGREGATE = "PIPELINE_TIMEOUT_AGGREGATE"  # 300s wall clock
    PIPELINE_MAX_ATTEMPTS_EXCEEDED = (
        "PIPELINE_MAX_ATTEMPTS_EXCEEDED"  # reconciler swept stuck PENDING
    )


class ProbeErrorCode(StrEnum):
    """Codes inside /readyz per-component JSON (NOT problem-details)."""

    PROBE_TIMEOUT = "PROBE_TIMEOUT"
    ES_INDEX_MISSING = "ES_INDEX_MISSING"
    DEPENDENCY_DOWN = "DEPENDENCY_DOWN"
    METRICS_DB_UNAVAILABLE = "METRICS_DB_UNAVAILABLE"
