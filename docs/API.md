# API Reference

Interactive docs (auto-generated from OpenAPI schema):
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

**Startup:** `uvicorn ragent.bootstrap.app:create_app --factory --host ${RAGENT_HOST:-0.0.0.0} --port ${RAGENT_PORT:-8000}`
(legacy: `python -m ragent.api` — delegates to the same factory)

The Swagger UI **Authorize** button drives every protected endpoint. The published security scheme tracks `RAGENT_AUTH_MODE` (T8.D1): for `none`/`user_header`/`jwt_prefer_header` modes the scheme is `UserIdHeader` pointing at `<RAGENT_USER_ID_HEADER>`; for `jwt_header` mode the scheme is `JWT` pointing at `<RAGENT_JWT_HEADER>` (default `X-Auth-Token`). Public paths (`/livez`, `/readyz`, `/startupz`, `/metrics`, `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json`) carry no security requirement.

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
| `application/pdf` | PDF | Per-page markdown via pymupdf4llm; RapidOCR for image-bearing pages |

CSV is not accepted.


### `POST /ingest/v1` — `ingest_type=inline` (content in body)

Cap: `INGEST_INLINE_MAX_BYTES` (default 10 MB) on the UTF-8 byte length of `content`.

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

The server reads from `(minio_site, object_key)` directly — no copy. `minio_site` must be a name configured in `MINIO_SITES`. Cap: `INGEST_FILE_MAX_BYTES` (default 50 MB) verified at API time via HEAD-probe.

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

Status values: `UPLOADED → PENDING → READY | FAILED`; `DELETING` during delete. `error_code`/`error_reason` are set when `status="FAILED"` (e.g. `EMBEDDER_ERROR`, `INGEST_ARCHIVE_UNSAFE`, `INGEST_PDF_TOO_MANY_PAGES`, `PIPELINE_TIMEOUT_AGGREGATE`).

### `GET /ingest/v1` — List documents (cursor-paginated)

Results ordered newest-first (`document_id DESC`). Query params: `limit` (default 100, max 100), `after` (cursor), `source_id`, `source_app`.

```bash
curl "http://localhost:8000/ingest/v1?limit=20&source_app=confluence" \
  -H "X-User-Id: user-123"
```

```json
// 200 OK
{
  "items": [{"document_id":"01J9…","status":"READY","source_id":"DOC-123",
             "source_app":"confluence","source_title":"Q3 OKR Planning",
             "updated_at":"2026-05-05T10:00:00.000Z"}],
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

### `POST /ingest/v1/{document_id}/rerun` — Manually re-dispatch the pipeline

Operator escape hatch for non-READY/non-DELETING documents. Flips row back to `PENDING` (clearing `error_code`/`error_reason`) and re-enqueues `ingest.pipeline`.

```bash
curl -X POST http://localhost:8000/ingest/v1/01J9ABCDEFGHJKMNPQRSTVWXYZ/rerun \
  -H "X-User-Id: user-123"
# 202 {"document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ"}
```

| Status | `error_code` | When |
|---|---|---|
| 404 | `INGEST_NOT_FOUND` | No document with that id. |
| 409 | `INGEST_NOT_RERUNNABLE` | Status is `READY` (use re-POST with same `source_id`/`source_app` for supersede) or `DELETING`. |

### `POST /ingest/v1/upload` — Multipart file upload (admin)

Server stages bytes to the default MinIO site; row carries `ingest_type="upload"`. Cap: `INGEST_INLINE_MAX_BYTES` (10 MB).

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

Required form fields: `file`, `source_id`, `source_app`, `source_title`, `mime_type`. Optional: `source_meta` (≤ 1024), `source_url` (≤ 2048).

---

## Chat

Request schema is shared by both endpoints. Only `messages` is required.

```json
{
  "messages": [{ "role": "user", "content": "What are our Q3 OKRs?" }],
  "provider": "openai",
  "model": "gptoss-120b",
  "temperature": null,
  "max_tokens": 4096,
  "source_app": "confluence",
  "source_meta": "engineering",
  "top_k": 20,
  "min_score": null,
  "dedupe": false,
  "context_mode": "auto"
}
```

`source_app` and `source_meta` are optional retrieval filters (AND when both supplied; omit to retrieve across all documents).

`top_k` (1–200, default `RETRIEVAL_TOP_K`=20) caps the number of chunks. `min_score` (default `null`) is a post-retrieval score floor.

### `POST /chat/v1` — Non-streaming chat

**Request fields** (all optional except `messages`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `messages` | array | — | Required. Conversation turns (`role`/`content`). |
| `provider` | string | `"openai"` | LLM provider (validated against `{"openai"}`). |
| `model` | string | env default | Model name forwarded to provider. |
| `temperature` | float\|null | `null` | `null` = intent-based auto (GREETING/CHITCHAT → 0.8, QUESTION/SUMMARY → 0.2, GENERATION → 0.7). |
| `max_tokens` | int | `4096` | Max completion tokens. |
| `source_app` | string | `null` | ES filter: restrict chunks to this source app. |
| `source_meta` | string | `null` | ES filter: restrict chunks to this source meta tag. |
| `top_k` | int | `20` | Max chunks to retrieve (1–200). |
| `min_score` | float | `null` | Minimum chunk score threshold. |
| `dedupe` | bool | `false` | Keep only the top-scored chunk per `document_id`. |
| `context_mode` | string | `"auto"` | `"auto"` = intent-based; `"caller"` = skip retrieval; `"force"` = always retrieve. Sending removed `retrieve` field returns 422. |

**Intent detection:** runs on every request (lightweight `temperature=0`, `max_tokens=10` LLM call) to classify the last user turn. In `"auto"` mode: GREETING/CHITCHAT → retrieval skipped; QUESTION/SUMMARY/GENERATION → retrieval runs. See spec §3.4.1 for full taxonomy.

**`sources` semantics:** `null` = retrieval skipped; `[]` = no hits; `[{…}]` = results found.

```bash
curl -X POST http://localhost:8000/chat/v1 \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{"messages": [{"role": "user", "content": "What are our Q3 OKRs?"}],
       "source_app": "confluence"}'
```

```json
// 200 OK
{
  "content": "根據所提供的資料，Q3 OKRs 包含...",
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
  ],
  "request_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
  "feedback_token": "<base64url>.<hmac_hex>"
}
```

`request_id` + `feedback_token` are emitted **only when `CHAT_FEEDBACK_ENABLED=true` AND `X-User-Id` present**. `content` is always a string (empty `""` if LLM returns null/missing). Full-width brackets `【N】` in LLM output are post-processed to `[N]`.

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
data: {"type": "delta", "content": " on the documents..."}
data: {"type": "done", "content": "Based on the documents...", "model": "gptoss-120b", "provider": "openai", "sources": [...], "request_id": "01J9...", "feedback_token": "…"}
```

`done` event omits `usage` (server-side logs only). Error events:
- `{"type": "error", "error_code": "LLM_STREAM_INTERRUPTED", "message": "..."}` — stream closed before `[DONE]`.
- `{"type": "error", "error_code": "LLM_ERROR"|"LLM_TIMEOUT", "message": "..."}` — upstream failure.

---

## ChatAgent

Three proxy endpoints under `/chatagent/v1` that forward requests to external services. All share `Authorization: <CHATAGENT_AUTH>` outbound header. Each is registered only when its URL env var is set.

### `POST /chatagent/v1` — Chat via external agent service

Same request body as `/chat/v1` plus optional `session` field (session ID; auto-generated when absent). Injects `user` (resolved via `RAGENT_JWT_CLAIM_USER_ID`, same as all other endpoints) and `userToken` (raw JWT) server-side. Forwards to `CHATAGENT_API_URL`.

```bash
curl -X POST http://localhost:8000/chatagent/v1 \
  -H "X-Auth-Token: <jwt>" -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What are our Q3 OKRs?"}]}'
```

```json
// 200 OK
{"session":"01JWTXYZ...","content":"根據所提供的資料，Q3 OKRs 包含...","usage":{"promptTokens":null,"completionTokens":null},"model":"gptoss-120b","provider":"openai","sources":null}
```

Errors: `429 CHATAGENT_RATE_LIMITED` · `502 CHATAGENT_UPSTREAM_ERROR` · `504 CHATAGENT_TIMEOUT`.

### `GET /chatagent/v1/sessionList` — List chat sessions

Proxies to `CHATAGENT_SESSIONLIST_API_URL`. Optional query params: `startTime`, `endTime` (ISO 8601).

```bash
curl "http://localhost:8000/chatagent/v1/sessionList?startTime=2025-05-01T00:00:00.000Z" \
  -H "X-Auth-Token: <jwt>"
```

```json
// 200 OK — {"totalCount":3,"sessions":[{"apName":"ragent","user":"alice","session":"abc123","updateTime":"...","sessionName":"Q3 OKR chat"}]}
```

### `GET /chatagent/v1/session` — Get session detail

Proxies to `CHATAGENT_SESSION_API_URL`. Required query param: `session`.

```bash
curl "http://localhost:8000/chatagent/v1/session?session=abc123" \
  -H "X-Auth-Token: <jwt>"
```

Returns session object with `messages[]` array (role, content, timestamps).

---

### `POST /chatagent/v2` — Raw-proxy chat (with streaming)

Accepts the upstream wire format directly. Client omits `apName`, `user`, and `userToken` — the server injects them before forwarding. Upstream response is forwarded byte-for-byte with no reshaping. Registered only when `CHATAGENT_API_URL` is set.

**Request body:**

```json
{
  "metadata": { "session": "optional-caller-id" },
  "inputData": { "message": "What are our Q3 OKRs?" },
  "stream": false
}
```

`metadata` is optional (session auto-generated when absent). `stream` defaults to `false`.

**Non-streaming (`stream: false`):**

```bash
curl -X POST http://localhost:8000/chatagent/v2 \
  -H "X-Auth-Token: <jwt>" -H "Content-Type: application/json" \
  -d '{"inputData": {"message": "What are our Q3 OKRs?"}, "stream": false}'
```

```json
// 200 OK — upstream JSON forwarded byte-for-byte
{"returnCode":96200,"returnData":{"messages":[{"role":"assistant","content":"根據所提供的資料...","message_id":"m1"}]}}
```

**Streaming (`stream: true`):**

```bash
curl -X POST http://localhost:8000/chatagent/v2 \
  -H "X-Auth-Token: <jwt>" -H "Content-Type: application/json" \
  -d '{"inputData": {"message": "Summarise the release notes."}, "stream": true}'
```

```
// 200 OK — Transfer-Encoding: chunked; each upstream chunk forwarded immediately
{"returnCode":96200,"returnData":{"delta":"The release notes "}}
{"returnCode":96200,"returnData":{"delta":"cover..."}}
{"returnCode":96200,"returnData":{"done":true}}
```

The response `Content-Type` is forwarded from the upstream response (e.g. `application/json`, `text/event-stream`). Upstream errors before the first byte return `502`/`504` — not `200` with an empty body.

Errors: `429 CHATAGENT_RATE_LIMITED` · `502 CHATAGENT_UPSTREAM_ERROR` · `504 CHATAGENT_TIMEOUT`.

---

## Retrieve

### `POST /retrieve/v1` — Retrieve chunks without LLM

Full retrieval pipeline (embed → kNN + BM25 → RRF → hydration) without invoking the LLM.

```bash
curl -X POST http://localhost:8000/retrieve/v1 \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{"query": "What are our Q3 OKRs?", "source_app": "confluence",
       "top_k": 10, "min_score": 0.3, "dedupe": true}'
```

```json
// 200 OK
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

**Request fields:** `query` (required), `source_app`, `source_meta`, `top_k` (1–200, default 20), `min_score`, `dedupe` (bool, default `false` — when `true`, keeps highest-scored chunk per `document_id`).

`excerpt` is the chunk text truncated to `EXCERPT_MAX_CHARS` (default 512) by `_ExcerptTruncator`. Same truncation applies to `sources[].excerpt` in chat responses.

---

## Feedback

### `POST /feedback/v1` — Record a vote against a chat source

Default disabled (`CHAT_FEEDBACK_ENABLED=false`). When enabled, drives `_FeedbackMemoryRetriever` (3rd RRF input). **Headers:** `X-User-Id` required.

```json
{
  "request_id":     "01J9...",
  "feedback_token": "<base64url>.<hmac_hex>",
  "query_text":     "what are our Q3 OKRs?",
  "shown_sources":  [{"source_app": "confluence", "source_id": "DOC-A"},
                     {"source_app": "confluence", "source_id": "DOC-B"}],
  "source_app":     "confluence",
  "source_id":      "DOC-A",
  "vote":           1,
  "reason":         "irrelevant",
  "position_shown": 0
}
```

- `request_id`/`feedback_token`: from prior `/chat/v1` response (token TTL 7 days; HMAC binds `request_id + user_id + sources_hash`).
- `vote` ∈ {+1, -1}; `reason` ∈ `irrelevant|hallucinated|outdated|incomplete|wrong_citation|other`; `position_shown` optional (0-based rank).
- Voted `(source_app, source_id)` MUST be in `shown_sources`.

**Response:** `204 No Content`.

| Status | `error_code` | When |
|---|---|---|
| 401 | `FEEDBACK_TOKEN_INVALID` | HMAC/request_id/user_id/sources mismatch or malformed token. |
| 410 | `FEEDBACK_TOKEN_EXPIRED` | Token `ts` outside 7-day window. |
| 422 | `FEEDBACK_SOURCE_INVALID` | Voted pair not in `shown_sources`. |
| 422 | `FEEDBACK_VALIDATION` | Schema violations (`vote ∉ {±1}`, reason outside enum, missing field). |

Dual-write: MariaDB `feedback` (truth) → ES `feedback_v1` (serving view). ES failure logs `feedback.es_write_failed` + increments `ragent_feedback_es_write_failed_total`; request still returns 204.

---

## Observability

| Endpoint | Description |
|---|---|
| `GET /livez` | Liveness probe — always 200 if process is up |
| `GET /startupz` | Startup probe — 503 until every dep probe has been green at least once; then permanently 200 |
| `GET /readyz` | Readiness probe — checks all dependencies (DB, ES, Redis, MinIO); 503 with problem+json on failure. Emits structlog events `probe.start`, `probe.ok` / `probe.failed` (with `error_code`, `detail`, `duration_ms`) per probe. |
| `GET /metrics` | Prometheus metrics (text/plain) |

---

## MCP (Phase 2)

`POST /mcp/v1` — Model Context Protocol server (JSON-RPC 2.0, spec `2024-11-05`). Exposes the corpus as a single `retrieve` tool. Full spec: [`docs/spec/mcp_server.md`](docs/spec/mcp_server.md).

| Method | Purpose |
|---|---|
| `initialize` | Capability negotiation. |
| `notifications/initialized` | Client signals init complete; server returns 204. |
| `tools/list` | Returns the `retrieve` tool with `inputSchema` and `annotations: {readOnlyHint: true}` (MCP 2025-03-26+). |
| `tools/call` | Invokes `retrieve`. Result `content[0].text` is `[資料來源 #N]`-formatted text. Unknown args → `-32602 MCP_TOOL_INPUT_INVALID`. |
| `ping` | Returns `{}`. |

Errors surface as JSON-RPC error envelopes with `data.error_code` (`MCP_PARSE_ERROR`, `MCP_INVALID_REQUEST`, `MCP_METHOD_NOT_FOUND`, `MCP_TOOL_NOT_FOUND`, `MCP_TOOL_INPUT_INVALID`, `MCP_TOOL_EXECUTION_FAILED`). Auth failures still use `application/problem+json`.

## Embedding Model Lifecycle (admin)

`POST /embedding/v1/{promote,cutover,rollback,commit,abort}` plus `GET /embedding/v1/state` and `GET /embedding/v1/cutover/preflight` drive a zero-downtime embedding-model swap (B50). State machine: `IDLE → promote → CANDIDATE → cutover → CUTOVER → {commit|rollback}`; `CANDIDATE → abort → IDLE`.

| Endpoint | Purpose | Success |
|---|---|---|
| `POST /embedding/v1/promote` body `{name,dim,api_url,model_arg}` | Open migration; PUT ES mapping + enable dual-write | `200 {state:"CANDIDATE", candidate, promoted_at}` |
| `POST /embedding/v1/cutover` body `{force?: bool}` | Switch reads to candidate | `200 {state:"CUTOVER", read, cutover_at, preflight}` |
| `POST /embedding/v1/rollback` | Revert reads to stable | `200 {state:"CANDIDATE", read:"stable", rolled_back_at}` |
| `POST /embedding/v1/commit` | Promote candidate to stable; retire old field | `200 {state:"IDLE", stable, committed_at}` |
| `POST /embedding/v1/abort` | Drop candidate | `200 {state:"IDLE", aborted, aborted_at}` |
| `POST /embedding/v1/backfill` | Enqueue backfill task | `200 {state, queued, stable_index, candidate_index}` |
| `GET /embedding/v1/state` | Registry snapshot | `200 {stable, candidate, read, retired}` |
| `GET /embedding/v1/cutover/preflight` | Run gates without action | `200 {pass, gates}` |

**Non-2xx cases:**
- `409 EMBEDDING_LIFECYCLE_INVALID_STATE` — all state-mutation endpoints when transition is invalid.
- `409 EMBEDDING_CUTOVER_PREFLIGHT_FAILED` — `/cutover` when hard gates fail; body carries `preflight` report with failed gate names and details.
- `422 EMBEDDING_INVALID_CONFIG` / `EMBEDDING_FIELD_NAME_COLLISION` — `/promote` validation failures.
- `503` — `/backfill` when broker is not wired; `/embedding/v1/state` as `EMBEDDING_REGISTRY_NOT_READY` when the registry has not completed its first refresh.

Cutover hard gates: `state_is_candidate`, `field_dim_matches`, `candidate_coverage` (≥ 99%), `dual_write_warmup` (≥ 2 × cache TTL). See [`docs/team/2026_05_15_embedding_model_lifecycle.md`](team/2026_05_15_embedding_model_lifecycle.md) for full semantics.
