# API Reference

Interactive docs (auto-generated from OpenAPI schema):
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

All endpoints return RFC 9457 problem+json on errors. `X-User-Id` header is recorded for audit in Phase 1.

## Ingest

`POST /ingest/v1` accepts a JSON body with discriminator `ingest_type ∈ {inline, file}`.
`POST /ingest/v1/upload` accepts `multipart/form-data` (admin convenience — server handles MinIO staging).
Supported MIME types (`mime_type`):

| MIME type | Format | Notes |
|---|---|---|
| `text/plain` | Plain text | UTF-8 |
| `text/markdown` | Markdown | AST-split by top-level block |
| `text/html` | HTML | Script/nav/aside stripped |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | DOCX | One atom per paragraph/table |
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` | PPTX | One atom per slide |

CSV is no longer accepted.


### `POST /ingest/v1` — `ingest_type=inline` (content in body)

Cap: `INGEST_INLINE_MAX_BYTES` (default 50 MB) on the UTF-8 byte length of `content`.

```bash
curl -X POST http://localhost:8000/ingest/v1 \
  -H "X-User-Id: user-123" -H "Content-Type: application/json" \
  -d '{
    "ingest_type":  "inline",
    "mime_type":    "text/markdown",
    "content":      "# Q3 OKRs\n\n```python\npool = create_pool()\n```",
    "source_id":    "DOC-123",
    "source_app":   "confluence",
    "source_title": "Q3 OKR Planning",
    "source_meta":  "engineering",
    "source_url":   "https://wiki.example/q3-okr"
  }'
```

### `POST /ingest/v1` — `ingest_type=file` (object in MinIO)

The server reads from `(minio_site, object_key)` directly — no copy, no post-READY delete (we don't own the object). `minio_site` must be a name configured in `MINIO_SITES`. Cap: `INGEST_FILE_MAX_BYTES` (default 50 MB) verified at API time via HEAD-probe.

```bash
curl -X POST http://localhost:8000/ingest/v1 \
  -H "X-User-Id: user-123" -H "Content-Type: application/json" \
  -d '{
    "ingest_type":  "file",
    "mime_type":    "text/html",
    "minio_site":   "tenant-eu-1",
    "object_key":   "reports/2025.html",
    "source_id":    "DOC-456",
    "source_app":   "s3-importer",
    "source_title": "Annual Report 2025",
    "source_url":   "https://example.com/reports/2025"
  }'
```

```json
// 202 Accepted (both forms)
{ "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ" }
```

The returned `document_id` is the same identifier used by `GET /ingest/v1/{document_id}` and `DELETE /ingest/v1/{document_id}`.

**Errors (RFC 9457 problem+json):**
- `415 INGEST_MIME_UNSUPPORTED` — `mime_type` not in allow-list.
- `413 INGEST_FILE_TOO_LARGE` — inline content or file size exceeds the cap.
- `422 INGEST_VALIDATION` — discriminator/required-field shape errors.
- `422 INGEST_MINIO_SITE_UNKNOWN` — `minio_site` not in registry.
- `422 INGEST_OBJECT_NOT_FOUND` — `(minio_site, object_key)` HEAD-probe miss.

### `GET /ingest/v1/{document_id}` — Get document status

```bash
curl http://localhost:8000/ingest/v1/01J9ABCDEFGHJKMNPQRSTVWXYZ \
  -H "X-User-Id: user-123"
```

```json
// 200 OK
{
  "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
  "status": "READY",
  "attempt": 1,
  "updated_at": "2026-05-05T10:00:00.000Z",
  "ingest_type": "inline",
  "minio_site": null,
  "source_id": "DOC-123",
  "source_app": "confluence",
  "source_title": "Q3 OKR Planning",
  "source_meta": "engineering",
  "source_url": "https://wiki.example/q3-okr",
  "error_code": null,
  "error_reason": null
}
```

Status values: `UPLOADED → PENDING → READY | FAILED`; `DELETING` during delete.
For `ingest_type=file` rows, `minio_site` is the registered site name (e.g. `tenant-eu-1`); for `ingest_type=inline` it is `null` and bytes were staged to `__default__`.

### `GET /ingest/v1` — List documents (cursor-paginated)

Results are ordered newest-first (`document_id DESC`). Pass `next_cursor` as `after` to fetch the next page of older items.

**Query parameters:**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `limit` | `int` | `100` | Max items per page (server cap: 100) |
| `after` | `string` | — | Cursor from previous page's `next_cursor` |
| `source_id` | `string` | — | Filter to a specific source document ID |
| `source_app` | `string` | — | Filter to a specific source application |

```bash
curl "http://localhost:8000/ingest/v1?limit=20&after=01J9...&source_app=confluence" \
  -H "X-User-Id: user-123"
```

```json
// 200 OK
{
  "items": [
    {
      "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
      "status": "READY",
      "source_id": "DOC-123",
      "source_app": "confluence",
      "source_title": "Q3 OKR Planning",
      "updated_at": "2026-05-05T10:00:00.000Z"
    }
  ],
  "next_cursor": "01J9..."
}
```

### `DELETE /ingest/v1/{document_id}` — Delete a document

Cascade-deletes chunks from ES and all plugin stores.

```bash
curl -X DELETE http://localhost:8000/ingest/v1/01J9ABCDEFGHJKMNPQRSTVWXYZ \
  -H "X-User-Id: user-123"
# 204 No Content
```

### `POST /ingest/v1/upload` — Multipart file upload (admin)

Admin convenience path: the caller POSTs file bytes directly; the server stages them to the default MinIO site and enqueues the pipeline — identical downstream behaviour to `ingest_type=inline` (server owns the object; `DELETE` cleans it up).

Cap: `INGEST_INLINE_MAX_BYTES` (default 50 MB). When the client includes `Content-Length` for the part, the size is rejected before the file is read into memory.

```bash
curl -X POST http://localhost:8000/ingest/v1/upload \
  -H "X-User-Id: user-123" \
  -F "file=@report.md;type=text/markdown" \
  -F "source_id=DOC-123" \
  -F "source_app=admin-cli" \
  -F "source_title=Q3 OKR Planning" \
  -F "mime_type=text/markdown" \
  -F "source_url=https://wiki.example/q3-okr" \
  -F "source_meta=engineering"
```

```json
// 202 Accepted
{ "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ" }
```

**Form fields:**

| Field | Required | Notes |
|---|---|---|
| `file` | Yes | File bytes (any MIME in allow-list) |
| `source_id` | Yes | Caller-supplied document identifier |
| `source_app` | Yes | Application namespace |
| `source_title` | Yes | Human-readable title |
| `mime_type` | Yes | Must be `text/plain`, `text/markdown`, or `text/html` |
| `source_meta` | No | Opaque label, max 1024 chars |
| `source_url` | No | Origin URL, max 2048 chars |

**Errors:**
- `413 INGEST_FILE_TOO_LARGE` — file exceeds `INGEST_INLINE_MAX_BYTES`.
- `422` — missing/invalid form fields (FastAPI validation).

---

## Chat

Request schema is shared by both endpoints. Only `messages` is required.

```json
{
  "messages": [
    { "role": "user", "content": "What are our Q3 OKRs?" }
  ],
  "provider": "openai",
  "model": "gptoss-120b",
  "temperature": 0.7,
  "max_tokens": 4096,
  "source_app": "confluence",
  "source_meta": "engineering",
  "top_k": 20,
  "min_score": null
}
```

`source_app` and `source_meta` are optional retrieval filters (AND when both supplied; omit to retrieve across all documents).

`top_k` (default `RETRIEVAL_TOP_K`, default 20, range 1–200) caps the number of chunks passed to the LLM context. `min_score` (default `RETRIEVAL_MIN_SCORE`, default `null`) is a post-retrieval score floor; chunks below this threshold are dropped before building LLM context. Both fields use the same semantics as `/retrieve/v1`.

### `POST /chat/v1` — Non-streaming chat

```bash
curl -X POST http://localhost:8000/chat/v1 \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{
    "messages": [{"role": "user", "content": "What are our Q3 OKRs?"}],
    "source_app": "confluence"
  }'
```

```json
// 200 OK
{
  "content": "Based on the Q3 OKR Planning document, your objectives are...",
  "usage": { "promptTokens": 512, "completionTokens": 128, "totalTokens": 640 },
  "model": "gptoss-120b",
  "provider": "openai",
  "sources": [
    {
      "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
      "source_app": "confluence",
      "source_id": "DOC-123",
      "source_meta": "engineering",
      "type": "knowledge",
      "source_title": "Q3 OKR Planning",
      "source_url": "https://wiki.example/q3-okr",
      "mime_type": "text/markdown",
      "excerpt": "Key results for Q3 include...",
      "score": 0.87
    }
  ]
}
```

### `POST /chat/v1/stream` — Streaming chat (SSE)

```bash
curl -X POST http://localhost:8000/chat/v1/stream \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{"messages": [{"role": "user", "content": "Summarise our roadmap"}]}' \
  --no-buffer
```

```
data: {"type": "delta", "content": "Based"}
data: {"type": "delta", "content": " on"}
data: {"type": "delta", "content": " the documents..."}
data: {"type": "done", "content": "Based on the documents...", "model": "gptoss-120b", "provider": "openai", "sources": [...]}
```

Error event: `{"type": "error", "error_code": "LLM_ERROR", "message": "..."}`

---

## Retrieve

### `POST /retrieve/v1` — Retrieve chunks without LLM

Runs the full retrieval pipeline (embed → kNN + BM25 → RRF join → source hydration) and returns ranked chunks directly, without invoking the LLM. Useful for debugging retrieval quality or building custom UIs.

By default returns **all ranked chunks** — a single document can appear multiple times if several of its chunks scored highly. Set `"dedupe": true` to keep only the best-scoring chunk per `document_id`.

```bash
curl -X POST http://localhost:8000/retrieve/v1 \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{
    "query": "What are our Q3 OKRs?",
    "source_app": "confluence",
    "source_meta": "engineering",
    "top_k": 10,
    "min_score": 0.3,
    "dedupe": true
  }'
```

```json
// 200 OK — dedupe=false (default): same document_id can repeat
{
  "chunks": [
    {
      "document_id": "01J9AAA",
      "source_app": "confluence",
      "source_id": "DOC-123",
      "source_meta": "engineering",
      "type": "knowledge",
      "source_title": "Q3 OKR Planning",
      "source_url": "https://wiki.example/q3-okr",
      "mime_type": "text/markdown",
      "excerpt": "Key results for Q3 include...",
      "score": 0.87
    }
  ]
}
```

**Request fields:**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | `string` | Yes | — | Retrieval query text |
| `source_app` | `string` | No | — | ES filter; omit for unrestricted retrieval |
| `source_meta` | `string` | No | — | ES filter; ANDed with `source_app` when both supplied |
| `top_k` | `int` | No | `RETRIEVAL_TOP_K` (default 20) | Max chunks to return; range 1–200 |
| `min_score` | `float` | No | — | Minimum retrieval score threshold; chunks below this are dropped |
| `dedupe` | `bool` | No | `false` | When `true`, keeps only the highest-scored chunk per `document_id` |

**How `excerpt` works:**

Each chunk stored in ES is the raw text segment produced by the indexing pipeline's splitter. The `excerpt` field in the response is that chunk's text, truncated to `EXCERPT_MAX_CHARS` characters (default `512`, configurable via env var) by `_ExcerptTruncator` before it reaches the router. Truncation is a hard character cut — no semantic boundary is preserved. The same truncation applies to `sources[].excerpt` in `/chat/v1` and `/chat/v1/stream` responses.

---

## Observability

| Endpoint | Description |
|---|---|
| `GET /livez` | Liveness probe — always 200 if process is up |
| `GET /readyz` | Readiness probe — checks all dependencies (DB, ES, Redis, MinIO) |
| `GET /metrics` | Prometheus metrics (text/plain) |

```bash
curl http://localhost:8000/readyz
# {"status":"ok"}

curl http://localhost:8000/metrics
# # HELP reconciler_tick_total ...
```

## MCP (Phase 2)

`POST /mcp/v1` — Model Context Protocol server speaking JSON-RPC 2.0
(spec `2024-11-05`). Exposes the corpus as a single tool `retrieve` so
external MCP-aware agents (Claude Desktop, Cursor, in-house agents) can
invoke the retrieval pipeline through the MCP standard.

Supported methods (full spec: `docs/00_spec.md` §3.8):

| Method | Direction | Purpose |
|---|---|---|
| `initialize` | client → server | Capability negotiation. |
| `notifications/initialized` | client → server (notification) | Client signals init complete; server returns 204. |
| `tools/list` | client → server | Returns the single `retrieve` tool with its inputSchema. |
| `tools/call` | client → server | Invokes the tool. Result `content[0].text` is JSON-stringified `{chunks:[...]}`. |
| `ping` | bidirectional | Returns `{}`. |

Errors surface as JSON-RPC error envelopes with `data.error_code` mapping
to the standard catalog (`MCP_PARSE_ERROR`, `MCP_INVALID_REQUEST`,
`MCP_METHOD_NOT_FOUND`, `MCP_TOOL_NOT_FOUND`, `MCP_TOOL_INPUT_INVALID`,
`MCP_TOOL_EXECUTION_FAILED`). Transport-layer failures (e.g. auth 401)
still come through as `application/problem+json`, not as JSON-RPC errors.
