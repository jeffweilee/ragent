# API Reference

Interactive docs (auto-generated from OpenAPI schema):
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

All endpoints return RFC 9457 problem+json on errors. `X-User-Id` header is recorded for audit in Phase 1.

## Ingest (v2 — JSON only)

`POST /ingest` accepts a JSON body with discriminator `ingest_type ∈ {inline, file}`.
Supported MIME types (`content_type`): `text/plain`, `text/markdown`, `text/html`. CSV is no longer accepted.

### `POST /ingest` — `ingest_type=inline` (content in body)

Cap: `INGEST_INLINE_MAX_BYTES` (default 50 MB) on the UTF-8 byte length of `content`.

```bash
curl -X POST http://localhost:8000/ingest \
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

### `POST /ingest` — `ingest_type=file` (object in MinIO)

The server reads from `(minio_site, object_key)` directly — no copy, no post-READY delete (we don't own the object). `minio_site` must be a name configured in `MINIO_SITES`. Cap: `INGEST_FILE_MAX_BYTES` (default 50 MB) verified at API time via HEAD-probe.

```bash
curl -X POST http://localhost:8000/ingest \
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

The returned `document_id` is the same identifier used by `GET /ingest/{document_id}` and `DELETE /ingest/{document_id}`.

**Errors (RFC 9457 problem+json):**
- `415 INGEST_MIME_UNSUPPORTED` — `mime_type` not in allow-list.
- `413 INGEST_FILE_TOO_LARGE` — inline content or file size exceeds the cap.
- `422 INGEST_VALIDATION` — discriminator/required-field shape errors.
- `422 INGEST_MINIO_SITE_UNKNOWN` — `minio_site` not in registry.
- `422 INGEST_OBJECT_NOT_FOUND` — `(minio_site, object_key)` HEAD-probe miss.

### `GET /ingest/{document_id}` — Get document status

```bash
curl http://localhost:8000/ingest/01J9ABCDEFGHJKMNPQRSTVWXYZ \
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

### `GET /ingest` — List documents (cursor-paginated)

Results are ordered newest-first (`document_id DESC`). Pass `next_cursor` as `after` to fetch the next page of older items.

**Query parameters:**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `limit` | `int` | `100` | Max items per page (server cap: 100) |
| `after` | `string` | — | Cursor from previous page's `next_cursor` |
| `source_id` | `string` | — | Filter to a specific source document ID |
| `source_app` | `string` | — | Filter to a specific source application |

```bash
curl "http://localhost:8000/ingest?limit=20&after=01J9...&source_app=confluence" \
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

### `DELETE /ingest/{document_id}` — Delete a document

Cascade-deletes chunks from ES and all plugin stores.

```bash
curl -X DELETE http://localhost:8000/ingest/01J9ABCDEFGHJKMNPQRSTVWXYZ \
  -H "X-User-Id: user-123"
# 204 No Content
```

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
  "source_meta": "engineering"
}
```

`source_app` and `source_meta` are optional retrieval filters (AND when both supplied; omit to retrieve across all documents).

### `POST /chat` — Non-streaming chat

```bash
curl -X POST http://localhost:8000/chat \
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

### `POST /chat/stream` — Streaming chat (SSE)

```bash
curl -X POST http://localhost:8000/chat/stream \
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

### `POST /retrieve` — Retrieve chunks without LLM

Runs the full retrieval pipeline (embed → kNN + BM25 → RRF join → source hydration) and returns ranked chunks directly, without invoking the LLM. Useful for debugging retrieval quality or building custom UIs.

By default returns **all ranked chunks** — a single document can appear multiple times if several of its chunks scored highly. Set `"dedupe": true` to keep only the best-scoring chunk per `document_id`.

```bash
curl -X POST http://localhost:8000/retrieve \
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

Each chunk stored in ES is the raw text segment produced by the indexing pipeline's splitter. The `excerpt` field in the response is that chunk's text, truncated to `EXCERPT_MAX_CHARS` characters (default `512`, configurable via env var) by `_ExcerptTruncator` before it reaches the router. Truncation is a hard character cut — no semantic boundary is preserved. The same truncation applies to `sources[].excerpt` in `/chat` and `/chat/stream` responses.

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

`POST /mcp/tools/rag` — Returns `501 MCP_NOT_IMPLEMENTED` in Phase 1.
