### 3.8 MCP Tool Server (P2.5)

Exposes ragent's retrieval pipeline as a **Model Context Protocol** tool so external LLM agents (Claude Desktop, Cursor, in-house agents) can call ragent's corpus through the MCP standard rather than a bespoke HTTP shape. The MCP server **wraps `POST /retrieve/v1`** (§3.4.4) — it does NOT call the LLM. The calling agent's own LLM does the synthesis; ragent supplies the grounded chunks.

**Decision (B47):** P2.5 implements a **real MCP server speaking JSON-RPC 2.0** (not the P1 stub's REST shape). The P1 `POST /mcp/v1/tools/rag` 501 endpoint is **removed** and replaced by `POST /mcp/v1` carrying JSON-RPC envelopes. This is the user-requested Option B (full MCP, retrieve-only). Option A (REST tool-call) and Option C (REST + thin MCP shim) were rejected because they either misrepresent the protocol (A) or carry two surfaces with the same behavior (C).

#### 3.8.1 Protocol

- **Transport:** Streamable HTTP, request/response subset (POST only; no server-initiated SSE in P2.5). Supported MCP spec revisions: `"2025-06-18"` (latest; first revision with tool `outputSchema` / `structuredContent` — required by §3.8.3's structured result), `"2025-03-26"`, `"2024-11-05"`. `initialize` echoes the client-requested revision when supported, otherwise answers with the latest (standard MCP version negotiation); the §3.8.3 `structuredContent` field is emitted regardless — older clients ignore additive result fields.
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
- **Auth:** governed by `RAGENT_AUTH_MODE` (§3.5): `jwt_header` mode expects `<RAGENT_JWT_HEADER>: <raw-jwt>` (joserfc-verified); `jwt_prefer_header` uses JWT when present then falls back to `<RAGENT_USER_ID_HEADER>`; `user_header` trusts `<RAGENT_USER_ID_HEADER>` directly (dev only); `none` injects `"anonymous"` (dev only). Auth applies before JSON-RPC dispatch; failure returns HTTP 401 with `application/problem+json` (NOT a JSON-RPC error — auth is a transport-layer concern).
- **Stateless mode:** P2.5 supports stateless requests only (no `Mcp-Session-Id` header). Stateful sessions deferred to P3 — gate condition: an MCP client requires server-initiated SSE or long-running tool resumption.
- **Request body cap:** `MCP_REQUEST_MAX_BYTES` (default 256 KiB); over-limit returns HTTP 413 `application/problem+json` (transport-layer, not JSON-RPC error).
- **Batch requests:** NOT implemented (P3 if needed). Array body → `-32600 Invalid Request`.

#### 3.8.2 Supported methods

| Method | Direction | Purpose |
|---|---|---|
| `initialize` | client → server | Capability negotiation. Returns `{protocolVersion, capabilities, serverInfo}`. |
| `notifications/initialized` | client → server (notification) | Client signals init complete. Server silently accepts. |
| `tools/list` | client → server | Returns `{tools: [{name, description, annotations?, inputSchema, outputSchema?}]}`. |
| `tools/call` | client → server | Invokes a tool. Returns `{content: [{type, text}], structuredContent, isError}`. |
| `ping` | bidirectional | Returns `{}`. Optional keepalive. |

Any other method → JSON-RPC error `-32601 Method not found`.

#### 3.8.3 The `retrieve` tool

One of two tools advertised by `tools/list` (the other is `AGENTIC_UI_TOOL`,
§3.8.3a). Mirrors §3.4.4 `POST /retrieve/v1` semantics:

```json
{
  "name": "retrieve",
  "description": "Retrieve ranked document chunks from the ragent knowledge corpus. Use when you need to ground a response in the organisation's internal documents — runs hybrid semantic + keyword search. Results are ordered by descending relevance. structuredContent.sources is the machine-readable source list: pass it to the UI's retrieved-sources panel. The text content is a <context>-delimited block with a citation table and [N] excerpt sections: ground your answer on the excerpts and cite by [N] — do NOT transcribe the <context> block verbatim into your reply. Does NOT synthesise an answer.",
  "annotations": {"readOnlyHint": true},
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "query":       {"type": "string",  "minLength": 1, "description": "Natural-language question or topic to search for. Write as a full question or statement rather than keyword strings — both semantic and keyword matching are applied."},
      "top_k":       {"type": "integer", "minimum": 1, "maximum": 200, "default": 20,   "description": "Maximum chunks to return, ranked by relevance (1–200, default 20). Increase for broad topics needing more evidence; decrease for focused lookups. Each chunk is typically 200–800 tokens."},
      "source_app":  {"type": "string",  "minLength": 1, "maxLength": 64,   "description": "Restrict results to documents from one source application (exact match, max 64 chars). Use a value from the `source_app` field in a previous retrieve result — omit on the first call to search across all sources."},
      "source_meta": {"type": "string",  "minLength": 1, "maxLength": 1024, "description": "Restrict results to documents tagged with this exact source_meta value (product, team, or category label; max 1024 chars). Omit to search without this filter."},
      "min_score":   {"type": "number",  "minimum": 0,                      "description": "Exclude chunks below this relevance score (≥ 0.0). Use 0.7 for high-confidence results only. Omit to return all top_k results regardless of score — recommended for exploratory queries."},
      "dedupe":      {"type": "boolean", "default": false, "description": "When true, return at most one chunk per source document (highest-scored). Set true for broad topic coverage across different documents; leave false to allow multiple excerpts from the same document."}
    },
    "required": ["query"]
  },
  "outputSchema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["sources"],
    "properties": {
      "sources": {
        "type": "array",
        "description": "Retrieved sources ordered by descending relevance. Pass this list to the UI's retrieved-sources panel.",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": ["document_id", "source_app", "source_id", "source_meta", "type", "source_title", "source_url", "mime_type", "excerpt", "score"],
          "properties": {
            "document_id":  {"type": ["string", "null"]},
            "source_app":   {"type": ["string", "null"]},
            "source_id":    {"type": ["string", "null"]},
            "source_meta":  {"type": ["string", "null"]},
            "type":         {"type": "string"},
            "source_title": {"type": ["string", "null"]},
            "source_url":   {"type": ["string", "null"]},
            "mime_type":    {"type": ["string", "null"]},
            "excerpt":      {"type": "string"},
            "score":        {"type": ["number", "null"]}
          }
        }
      }
    }
  }
}
```

`additionalProperties: false` makes this a closed schema — unknown arguments are rejected with `-32602 MCP_TOOL_INPUT_INVALID` rather than silently ignored.

`annotations.readOnlyHint=true` signals that `retrieve` never writes data. MCP hosts (protocol
2025-03-26+) MAY use this to skip confirmation prompts. Clients on earlier versions silently
ignore unknown tool fields — this is an additive, backward-compatible extension.

**`tools/call` result shape** (MCP 2025-06-18 structured tool output — a tool declaring `outputSchema` MUST return conforming `structuredContent`; the spec's SHOULD-recommendation of serialized JSON in the text block is intentionally replaced by a markdown digest, which is equally compliant and serves both LLM grounding and direct user display):

```json
{
  "content": [{"type": "text", "text": "<context>\n| # | 資料來源 | 來源系統 |\n|---|---------|---------|\n| 1 | [User Manual](https://wiki/abc) | confluence |\n\n### [1] User Manual\n> <excerpt up to EXCERPT_MAX_CHARS>\n</context>"}],
  "structuredContent": {"sources": [{"document_id": "abc123", "source_app": "confluence", "source_id": "SRC-1", "source_meta": "engineering", "type": "knowledge", "source_title": "User Manual", "source_url": "https://wiki/abc", "mime_type": "text/plain", "excerpt": "...", "score": 0.95}]},
  "isError": false
}
```

- **`structuredContent.sources`** — full source entries (`doc_to_source_entry()` output, identical to `POST /retrieve/v1` `sources`), validating against the advertised `outputSchema`. This is the machine-readable channel: the calling agent passes it to the frontend's retrieved-sources panel; no re-parsing of the text block.
- **`content[0].text`** — `<context>…</context>`-wrapped markdown digest with **zero natural-language wording** (no `Found N chunk(s).` preamble), so calling LLMs treat it as injected context data rather than prose to transcribe:
  - a user-presentable **citation table** (columns `#` / `資料來源` / `來源系統`; `資料來源` is `[title](source_url)` when `source_url` exists, plain title otherwise, `(未命名)` when the title is null). Internal fields (`document_id`, `score`, `source_id`, `mime_type`, `source_meta`) are NOT exposed in the text — they live in `structuredContent` only;
  - one `### [N] <title>` heading + blockquoted excerpt per source, for LLM grounding with `[N]` citation.
  - **Sanitisation:** table cells and headings have CR/LF stripped and `|` escaped to `\|` — a malicious title cannot inject fake rows or `### [N]` headings. Only `http(s)` `source_url` values are linkified (a `javascript:` URL renders as plain title text), with `(` `)` space `|` percent-encoded so the link destination cannot end early. Literal `<context>`/`</context>` tags inside titles/excerpts are neutralised to `&lt;…&gt;` so corpus text cannot close the wrapper. Raw values survive untouched in `structuredContent`.
  - Empty results return `<context>\n</context>` with `structuredContent: {"sources": []}`.

`isError: true` is set when the tool itself fails (e.g. retrieval pipeline raises); transport-layer failures still come through `error` envelopes.

#### 3.8.3a The `AGENTIC_UI_TOOL` tool

A **client-side** dispatcher advertised by `tools/list` so the `/chatagent/v3`
upstream (which only invokes pre-registered tools) can call per-request frontend
tools (§3.4.7). `inputSchema` is `{type:object, required:[tool_name, arguments],
properties:{tool_name:string, arguments:object}}`. The upstream chooses a frontend
tool from the `<tools>` catalog injected into the user turn, then emits an
`AGENTIC_UI_TOOL` call and **suspends**; ragent's ADK relay unwraps the envelope so
the frontend executes the real tool. It is **never executed server-side**: a
`tools/call AGENTIC_UI_TOOL` returns a soft `{isError: true}` result (not a
JSON-RPC error, not retrieval) directing the caller to execute it on the frontend.

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

- **S58 mcp initialize** — `initialize` with `protocolVersion:"2025-06-18"` → `result.{protocolVersion:"2025-06-18", capabilities:{tools:{}}, serverInfo:{name:"ragent",version:"<semver>"}}`. A supported older revision (`2025-03-26` / `2024-11-05`) is echoed back; an unsupported revision falls back to `2025-06-18`.
- **S59 mcp tools/list** — `result.tools` advertises `retrieve` (with `inputSchema`/`outputSchema` matching §3.8.3) and the `AGENTIC_UI_TOOL` client-side dispatcher (§3.8.3a).
- **S59a mcp tools/call AGENTIC_UI_TOOL** — Given `tools/call` with `{name:"AGENTIC_UI_TOOL", arguments:{...}}`, Then the result is a soft `{isError: true}` (no JSON-RPC error, retrieval pipeline not invoked) — the dispatcher is client-side (§3.8.3a).
- **S60 mcp tools/call retrieve** — Given indexed corpus and `tools/call` with `{name:"retrieve", arguments:{query:"...",top_k:3}}`, When the server processes it, Then `result.structuredContent.sources` carries one full source entry per chunk (N ≤ 3) validating against `outputSchema`, `result.content[0].text` is the `<context>`-wrapped citation table + `### [N]` excerpt blocks with no natural-language wording and no internal fields, and `result.isError` is `false`. Empty results return `structuredContent: {sources: []}` with `<context>\n</context>`.
- **S60a mcp tools/call retrieve unknown arg** — Given `tools/call` with `{name:"retrieve", arguments:{query:"q", unknown_field:"bad"}}`, Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_INPUT_INVALID`.
- **S61 mcp method not found** — Given `{method:"resources/list"}` (unimplemented), Then `error.code` is `-32601`.
- **S62 mcp tools/call invalid name** — Given `{method:"tools/call", params:{name:"unknown_tool",arguments:{}}}`, Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_NOT_FOUND`.
- **S63 mcp tools/call missing query** — Given `{method:"tools/call", params:{name:"retrieve",arguments:{}}}` (no `query`), Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_INPUT_INVALID`.
- **S64 mcp parse error** — Given a request body that is not valid JSON, Then HTTP `200` with JSON-RPC body `{jsonrpc:"2.0",id:null,error:{code:-32700,...}}` (per JSON-RPC 2.0 §5: `id` is `null` when parse failed).
- **S65 mcp notifications/initialized** — Given `{jsonrpc:"2.0", method:"notifications/initialized"}` (no `id`), Then HTTP `204` with empty body; no JSON-RPC response object emitted.
- **S66 mcp auth required** — Given `RAGENT_AUTH_MODE=jwt_header` and no `<RAGENT_JWT_HEADER>` header, Then HTTP `401` with `application/problem+json` (NOT a JSON-RPC error envelope) and `error_code=AUTH_TOKEN_INVALID`.
- **S67 mcp tool retrieval failure** — Given the retrieval pipeline raises, When `tools/call retrieve` is invoked, Then JSON-RPC response is `{error:{code:-32001, message:..., data:{error_code:"MCP_TOOL_EXECUTION_FAILED"}}}` — NOT `isError:true` inside a successful result. (App-error vs tool-soft-error distinction: pipeline crashes are JSON-RPC errors; an empty-result-set retrieval is `isError:false` with empty `chunks`.)
