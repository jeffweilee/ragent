### 3.8 MCP Tool Server (P2.5)

Exposes ragent's retrieval pipeline as a **Model Context Protocol** tool so external LLM agents (Claude Desktop, Cursor, in-house agents) can call ragent's corpus through the MCP standard rather than a bespoke HTTP shape. The MCP server **wraps `POST /retrieve/v1`** (§3.4.4) — it does NOT call the LLM. The calling agent's own LLM does the synthesis; ragent supplies the grounded chunks.

**Decision (B47):** P2.5 implements a **real MCP server speaking JSON-RPC 2.0** (not the P1 stub's REST shape). The P1 `POST /mcp/v1/tools/rag` 501 endpoint is **removed** and replaced by `POST /mcp/v1` carrying JSON-RPC envelopes. This is the user-requested Option B (full MCP, retrieve-only). Option A (REST tool-call) and Option C (REST + thin MCP shim) were rejected because they either misrepresent the protocol (A) or carry two surfaces with the same behavior (C).

#### 3.8.1 Protocol

- **Transport:** Streamable HTTP, request/response subset (POST only; no server-initiated SSE in P2.5). Pinned MCP spec revision: `"2024-11-05"`.
- **Endpoint:** `POST /mcp/v1` (single endpoint; method dispatched from JSON-RPC `method` field).
- **Envelope:** JSON-RPC 2.0:
  ```json
  // Request
  {"jsonrpc": "2.0", "id": <int|str|null>, "method": "<method>", "params": {...}}
  // Success response
  {"jsonrpc": "2.0", "id": <same-as-request>, "result": {...}}
  // Error response
  {"jsonrpc": "2.0", "id": <same-as-request>, "error": {"code": <int>, "message": "<text>", "data": {...}?}}
  ```
- **Notification** (no response): omit `id`. P2.5 supports `notifications/initialized` only.
- **Auth:** `<RAGENT_JWT_HEADER>: <raw-jwt>` (P2.2 onwards, joserfc-verified per §3.5) or `<RAGENT_USER_ID_HEADER>` fallback (`RAGENT_TRUST_X_USER_ID_HEADER=true`, dev only). Auth applies before JSON-RPC dispatch; failure returns HTTP 401 with `application/problem+json` (NOT a JSON-RPC error — auth is a transport-layer concern).
- **Stateless mode:** P2.5 supports stateless requests only (no `Mcp-Session-Id` header). Stateful sessions deferred to P3 — gate condition: an MCP client requires server-initiated SSE or long-running tool resumption.
- **Request body cap:** `MCP_REQUEST_MAX_BYTES` (default 256 KiB); over-limit returns HTTP 413 `application/problem+json` (transport-layer, not JSON-RPC error).
- **Batch requests:** NOT implemented (P3 if needed). Array body → `-32600 Invalid Request`.

#### 3.8.2 Supported methods

| Method | Direction | Purpose |
|---|---|---|
| `initialize` | client → server | Capability negotiation. Returns `{protocolVersion, capabilities, serverInfo}`. |
| `notifications/initialized` | client → server (notification) | Client signals init complete. Server silently accepts. |
| `tools/list` | client → server | Returns `{tools: [{name, description, annotations?, inputSchema}]}`. |
| `tools/call` | client → server | Invokes a tool. Returns `{content: [{type, text}], isError}`. |
| `ping` | bidirectional | Returns `{}`. Optional keepalive. |

Any other method → JSON-RPC error `-32601 Method not found`.

#### 3.8.3 The `retrieve` tool

The sole tool advertised by `tools/list`. Mirrors §3.4.4 `POST /retrieve/v1` semantics:

```json
{
  "name": "retrieve",
  "description": "Retrieve relevant document chunks from the ragent corpus using hybrid vector+BM25 search with optional reranking. Returns ranked chunks (no LLM synthesis).",
  "annotations": {"readOnlyHint": true},
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":       {"type": "string", "minLength": 1, "description": "Natural-language query."},
      "top_k":       {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
      "source_app":  {"type": "string",  "minLength": 1, "maxLength": 64,   "description": "Optional ES term filter."},
      "source_meta": {"type": "string",  "minLength": 1, "maxLength": 1024, "description": "Optional ES term filter."},
      "min_score":   {"type": "number",  "minimum": 0,    "description": "Optional post-pipeline score floor."},
      "dedupe":      {"type": "boolean", "default": false, "description": "Keep one chunk per document_id."}
    },
    "required": ["query"]
  }
}
```

`annotations.readOnlyHint=true` signals that `retrieve` never writes data. MCP hosts (protocol
2025-03-26+) MAY use this to skip confirmation prompts. Clients on earlier versions silently
ignore unknown tool fields — this is an additive, backward-compatible extension.

**`tools/call` result shape** (MCP spec compliant):
```json
{
  "content": [
    {"type": "text", "text": "{\"chunks\":[{...},{...}]}"}
  ],
  "isError": false
}
```

The single `content[0].text` value is the **JSON-stringified** `RetrieveResponse` (same shape as `POST /retrieve/v1`). MCP standardises tool-result content as a typed array; text type with stringified JSON is the canonical pattern for structured returns (the calling LLM parses it). `isError: true` is set when the tool itself fails (e.g. retrieval pipeline raises); transport-layer failures still come through `error` envelopes.

#### 3.8.4 Error codes (JSON-RPC layer)

| Code | Meaning | Origin |
|---|---|---|
| `-32700` | Parse error (malformed JSON) | Transport |
| `-32600` | Invalid Request (missing `jsonrpc` / `method`, etc.) | Transport |
| `-32601` | Method not found | Dispatch |
| `-32602` | Invalid params (e.g. `tools/call` with unknown `name`, or `inputSchema` validation fail) | Dispatch |
| `-32603` | Internal error | Server |
| `-32001` | Tool execution failed (retrieval pipeline error; mirrors `MCP_TOOL_EXECUTION_FAILED`) | App |

App-level errors (-32000..-32099) carry `data.error_code` matching the existing `HttpErrorCode` catalog (§4.1.2) so operators correlate JSON-RPC errors with HTTP errors. Example:
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"retrieval pipeline failed","data":{"error_code":"MCP_TOOL_EXECUTION_FAILED"}}}
```

#### 3.8.5 BDD

- **S58 mcp initialize** — `initialize` with `protocolVersion:"2024-11-05"` → `result.{protocolVersion:"2024-11-05", capabilities:{tools:{}}, serverInfo:{name:"ragent",version:"<semver>"}}`.
- **S59 mcp tools/list** — `result.tools` has exactly one entry `name:"retrieve"` with `inputSchema` matching §3.8.3.
- **S60 mcp tools/call retrieve** — Given indexed corpus and `tools/call` with `{name:"retrieve", arguments:{query:"...",top_k:3}}`, When the server processes it, Then `result.content[0].text` is JSON parseable into `{chunks: list}` of length ≤ 3 and `result.isError` is `false`.
- **S61 mcp method not found** — Given `{method:"resources/list"}` (unimplemented), Then `error.code` is `-32601`.
- **S62 mcp tools/call invalid name** — Given `{method:"tools/call", params:{name:"unknown_tool",arguments:{}}}`, Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_NOT_FOUND`.
- **S63 mcp tools/call missing query** — Given `{method:"tools/call", params:{name:"retrieve",arguments:{}}}` (no `query`), Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_INPUT_INVALID`.
- **S64 mcp parse error** — Given a request body that is not valid JSON, Then HTTP `200` with JSON-RPC body `{jsonrpc:"2.0",id:null,error:{code:-32700,...}}` (per JSON-RPC 2.0 §5: `id` is `null` when parse failed).
- **S65 mcp notifications/initialized** — Given `{jsonrpc:"2.0", method:"notifications/initialized"}` (no `id`), Then HTTP `204` with empty body; no JSON-RPC response object emitted.
- **S66 mcp auth required** — Given `RAGENT_AUTH_DISABLED=false` and `RAGENT_TRUST_X_USER_ID_HEADER=false` and no `<RAGENT_JWT_HEADER>` header, Then HTTP `401` with `application/problem+json` (NOT a JSON-RPC error envelope) and `error_code=AUTH_TOKEN_INVALID`.
- **S67 mcp tool retrieval failure** — Given the retrieval pipeline raises, When `tools/call retrieve` is invoked, Then JSON-RPC response is `{error:{code:-32001, message:..., data:{error_code:"MCP_TOOL_EXECUTION_FAILED"}}}` — NOT `isError:true` inside a successful result. (App-error vs tool-soft-error distinction: pipeline crashes are JSON-RPC errors; an empty-result-set retrieval is `isError:false` with empty `chunks`.)
