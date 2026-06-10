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

### `PUT /chatagent/v1/session` — Rename a session

Proxies to `CHATAGENT_SESSION_API_URL`. Request body: `{"session": "<id>", "sessionName": "<new name>"}`. Server injects `apName` and `user` before forwarding.

```bash
curl -X PUT http://localhost:8000/chatagent/v1/session \
  -H "Content-Type: application/json" \
  -H "X-User-Id: alice" \
  -d '{"session": "abc123", "sessionName": "My Chat"}'
```

Returns the upstream response unchanged (including `204 No Content` when the upstream returns no body). Registered only when `CHATAGENT_SESSION_API_URL` is set.

Errors: `502 CHATAGENT_UPSTREAM_ERROR` · `504 CHATAGENT_TIMEOUT`.

### `DELETE /chatagent/v1/session` — Delete a session

Proxies to `CHATAGENT_SESSION_API_URL`. Request body: `{"session": "<id>"}`. Server injects `apName` and `user` before forwarding.

```bash
curl -X DELETE http://localhost:8000/chatagent/v1/session \
  -H "Content-Type: application/json" \
  -H "X-User-Id: alice" \
  -d '{"session": "abc123"}'
```

Returns the upstream response unchanged (including `204 No Content` when the upstream returns no body). Registered only when `CHATAGENT_SESSION_API_URL` is set.

Errors: `502 CHATAGENT_UPSTREAM_ERROR` · `504 CHATAGENT_TIMEOUT`.

---

### `POST /chatagent/v2` — Raw-proxy chat (with streaming)

Accepts any JSON body. The server injects `apName`, `user`, and `userToken` into `metadata` before forwarding; all other fields are passed through verbatim. Upstream response is forwarded byte-for-byte with no reshaping. Registered only when `CHATAGENT_API_URL` is set.

**Request body** (flexible — any JSON object):

```json
{
  "metadata": { "session": "optional-caller-id" },
  "inputData": { "message": "What are our Q3 OKRs?", "messageMeta": { "lang": "en" } },
  "stream": false
}
```

`metadata` is optional (session auto-generated when absent). If `metadata` is not a JSON object it is ignored and treated as absent. `stream` defaults to `false`. Any extra fields at any level are forwarded unchanged.

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

Errors: `429 CHATAGENT_RATE_LIMITED` · `502 CHATAGENT_UPSTREAM_ERROR` · `504 CHATAGENT_TIMEOUT`. Request timeout defaults to `CHATAGENT_TIMEOUT_SECONDS` (default 30 s).

### `POST /chatagent/v3` — twp-ai protocol over the ChatAgent upstream (SSE)

Same upstream as v2 (`CHATAGENT_API_URL`, `CHATAGENT_AUTH`, rate limit, `CHATAGENT_TIMEOUT_SECONDS`), but the wire contract is the **twp-ai protocol**: the request is a twp-ai `RunAgentInput` and the response is a twp-ai SSE event stream. ragent converts both directions — it builds the v2 upstream payload from the run input, then maps the upstream's SSE stream into twp-ai AG-UI events. Registered only when `CHATAGENT_API_URL` is set.

**Conversion rules:**

- **Request → upstream:** the upstream is a general, tool-capable agent with its own persona and conversation memory (keyed by `session`), so v3 imposes no persona and does not enumerate tools — it only folds the client-supplied `context`/`state` that the single-field wire would otherwise drop. A labelled preamble (`Context: {json}` and/or `State: {json}`) is prepended to the last `role="user"` message content and the combined text becomes `inputData.message`; with no context and no state the message is the bare user text. `metadata` is server-injected (`apName`/`user`/`userToken`/`session`) with `session = threadId`; `stream` is always `true`. `model` is not forwarded (the upstream decides, as in v2). `tools`/`forwardedProps` are accepted but not forwarded; client tool-call continuation is not yet handled.
- **Upstream → response:** each SSE line is `data: {json}\n\n`; `returnData.messages[].content` → `TEXT_MESSAGE_CONTENT` (bracketed by `TEXT_MESSAGE_START`/`TEXT_MESSAGE_END`; `messageId` taken from upstream `messages[].messageId`). Each distinct upstream node gets its own block; the `planner` node (`messageMeta.langgraph_node`) is the plan/reasoning step and is bracketed by `REASONING_START`/`REASONING_MESSAGE_START`/`REASONING_MESSAGE_CONTENT`/`REASONING_MESSAGE_END`/`REASONING_END` instead, while other nodes (commander/summarizer) produce TEXT_MESSAGE blocks. `finish_reason="tool_calls"` + `tool_calls` → `TOOL_CALL_START/ARGS/END`; `role="tool"` turns → `TOOL_CALL_RESULT`. `humanInTheLoopMeta.isInterrupt=true` → standalone TEXT_MESSAGE with `interruptMessage` as delta. `data: [Done]` sentinel → `RUN_FINISHED`.
- **Errors are events, not HTTP codes:** rate-limit, upstream non-`96200`, 5xx, and timeout all surface as a single `RUN_ERROR` event over a `200` stream (`code` = `CHATAGENT_RATE_LIMITED` / `CHATAGENT_UPSTREAM_ERROR` / `CHATAGENT_TIMEOUT`). This is a **breaking change** from v2's HTTP `429`/`502`/`504`.

**Request body** (twp-ai `RunAgentInput`; required: `threadId`, `runId`, `messages`, `tools`, `state`, `context`, `forwardedProps`):

```json
{
  "threadId": "thread_1",
  "runId": "run_1",
  "messages": [{ "id": "m1", "role": "user", "content": "Summarise the release notes." }],
  "tools": [],
  "state": null,
  "context": [],
  "forwardedProps": null
}
```

```bash
curl -X POST http://localhost:8000/chatagent/v3 \
  -H "X-Auth-Token: <jwt>" -H "Content-Type: application/json" \
  -d '{"threadId":"thread_1","runId":"run_1","messages":[{"id":"m1","role":"user","content":"Summarise the release notes."}],"tools":[],"state":null,"context":[],"forwardedProps":null}' \
  --no-buffer
```

**Response:** `text/event-stream`. Every upstream `data: {json}` SSE line passes through the conversion pipeline and becomes one or more AG-UI events on the response stream. The stream always opens with `RUN_STARTED` and closes with `RUN_FINISHED` (success) or `RUN_ERROR` (any failure).

#### Example 1 — Simple text reply

Upstream emits a single assistant message in chunks; ragent wraps it in a TEXT_MESSAGE block.

```
# upstream (ChatAgent SSE — not visible to clients)
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"msg-1","role":"assistant","content":"The release notes "}]}}
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"msg-1","role":"assistant","content":"cover three areas."}]}}
data: [Done]

# ragent response (twp-ai AG-UI SSE)
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}

data: {"type":"TEXT_MESSAGE_START","messageId":"msg-1","role":"assistant"}

data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-1","delta":"The release notes "}

data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-1","delta":"cover three areas."}

data: {"type":"TEXT_MESSAGE_END","messageId":"msg-1"}

data: {"type":"RUN_FINISHED","runId":"run_1","threadId":"thread_1"}
```

#### Example 2 — Multi-agent (planner → commander → summarizer)

Each upstream `messageId` maps to an independent block. `messageMeta.langgraph_node` (carried in `UpstreamMessage.agent_type`) selects the block type: the **`planner`** node is the agent's plan/reasoning step, surfaced as a `REASONING_*` block (`REASONING_START` → `REASONING_MESSAGE_START`/`CONTENT`*/`END` → `REASONING_END`); every other node (`commander`, `summarizer`, …) becomes a `TEXT_MESSAGE` block.

```
# upstream
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"plan-1","role":"assistant","content":"Planning...","messageMeta":{"langgraph_node":"planner"}}]}}
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"cmd-1","role":"assistant","content":"Executing step 1.","messageMeta":{"langgraph_node":"commander"}}]}}
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"sum-1","role":"assistant","content":"Done.","messageMeta":{"langgraph_node":"summarizer"}}]}}
data: [Done]

# ragent response
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}

data: {"type":"REASONING_START"}
data: {"type":"REASONING_MESSAGE_START","messageId":"plan-1","role":"reasoning"}
data: {"type":"REASONING_MESSAGE_CONTENT","messageId":"plan-1","delta":"Planning..."}
data: {"type":"REASONING_MESSAGE_END","messageId":"plan-1"}
data: {"type":"REASONING_END"}

data: {"type":"TEXT_MESSAGE_START","messageId":"cmd-1","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"cmd-1","delta":"Executing step 1."}
data: {"type":"TEXT_MESSAGE_END","messageId":"cmd-1"}

data: {"type":"TEXT_MESSAGE_START","messageId":"sum-1","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"sum-1","delta":"Done."}
data: {"type":"TEXT_MESSAGE_END","messageId":"sum-1"}

data: {"type":"RUN_FINISHED","runId":"run_1","threadId":"thread_1"}
```

#### Example 3 — Tool call + tool result

`finish_reason="tool_calls"` + `tool_calls` → `TOOL_CALL_START/ARGS/END`; a subsequent `role="tool"` message → `TOOL_CALL_RESULT`. Each upstream `tool_calls[]` element carries an `id` field — ragent uses that directly as the AG-UI `toolCallId`. If the upstream omits `id` (legacy), ragent falls back to `{messageId}-{index}`. Tool results are correlated back to their call via a FIFO queue keyed on `displayMeta.toolName`.

```
# upstream
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"msg-tc","role":"assistant","content":null,"finish_reason":"tool_calls","tool_calls":[{"id":"call-abc","type":"function","function":{"name":"search","arguments":"{\"query\":\"release notes\"}"}}],"displayMeta":{"toolName":"search"}}]}}
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"msg-tr","role":"tool","content":"Found 3 results.","displayMeta":{"toolName":"search"}}]}}
data: [Done]

# ragent response
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}

data: {"type":"TOOL_CALL_START","toolCallId":"call-abc","toolCallName":"search","parentMessageId":"msg-tc"}

data: {"type":"TOOL_CALL_ARGS","toolCallId":"call-abc","delta":"{\"query\":\"release notes\"}"}

data: {"type":"TOOL_CALL_END","toolCallId":"call-abc"}

data: {"type":"TOOL_CALL_RESULT","messageId":"msg-tr","toolCallId":"call-abc","content":"Found 3 results."}

data: {"type":"RUN_FINISHED","runId":"run_1","threadId":"thread_1"}
```

#### Example 4 — Human-in-the-loop interrupt

`humanInTheLoopMeta.isInterrupt=true` surfaces as a standalone TEXT_MESSAGE containing `interruptMessage` as the delta.

```
# upstream
data: {"returnCode":96200,"returnData":{"messages":[{"messageId":"hitl-1","role":"assistant","content":null,"humanInTheLoopMeta":{"isInterrupt":true,"interruptMessage":"Please confirm before deleting all records.","interruptContent":"ctx"}}]}}
data: [Done]

# ragent response
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}

data: {"type":"TEXT_MESSAGE_START","messageId":"hitl-1","role":"assistant"}

data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"hitl-1","delta":"Please confirm before deleting all records."}

data: {"type":"TEXT_MESSAGE_END","messageId":"hitl-1"}

data: {"type":"RUN_FINISHED","runId":"run_1","threadId":"thread_1"}
```

#### Example 5 — Error scenarios

All errors surface as `RUN_ERROR` over the same `200 text/event-stream` response (no HTTP error codes for mid-stream failures).

```
# upstream non-96200 (e.g. quota exceeded)
data: {"returnCode":96500,"returnMessage":"quota exceeded","returnData":{}}
data: [Done]

# ragent response
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}
data: {"type":"RUN_ERROR","message":"quota exceeded","code":"CHATAGENT_UPSTREAM_ERROR","runId":"run_1","threadId":"thread_1"}
```

```
# upstream timeout (httpx.TimeoutException)

# ragent response
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}
data: {"type":"RUN_ERROR","message":"chatagent upstream failed: timed out","code":"CHATAGENT_TIMEOUT","runId":"run_1","threadId":"thread_1"}
```

```
# truncated stream (upstream closes without [Done])

# ragent response
data: {"type":"RUN_STARTED","runId":"run_1","threadId":"thread_1"}
data: {"type":"RUN_ERROR","message":"upstream closed stream without [Done] sentinel","code":"CHATAGENT_UPSTREAM_ERROR","runId":"run_1","threadId":"thread_1"}
```

> v3 reuses the twp-ai `Agent`/caller abstraction: an `ADKAgent` (in `packages/twp-ai`) owns the event flow, and a ragent-side `ADKCaller` (`src/ragent/clients/adk_caller.py`) does the upstream proxy. This is a separate service from `/twp/v1/run` (which is a native agent host); the two are unrelated.

---

## twp-ai

Agent-User Interaction adapter for page-aware, client-tool runs (`packages/twp-ai`), mounted at `/twp/v1`. Emits twp-ai camelCase SSE events. Standard auth applies (`X-User-Id` or `X-Auth-Token`, same as other endpoints).

### `POST /twp/v1/run` — Page-aware agent run (SSE)

**Request body** (camelCase). Required: `threadId`, `runId`, `messages`, `tools`, `state`, `context`, `forwardedProps`. Optional: `parentRunId`, `model` (falls back to `TWP_DEFAULT_MODEL` when omitted). `tools` carries the page's client-side tool definitions; `context` carries page facts; `state` is the current app/page state.

```json
{
  "threadId": "thread_1",
  "runId": "run_1",
  "state": { "page": { "title": "Edit product" } },
  "messages": [{ "id": "msg_1", "role": "user", "content": "Fill the description" }],
  "tools": [
    {
      "name": "fill_form",
      "description": "Use only when the user asks to fill or update form fields.",
      "parameters": { "type": "object", "properties": { "description": { "type": "string" } } }
    }
  ],
  "context": [{ "description": "Current page", "value": "{\"title\":\"Edit product\",\"fields\":[\"description\"]}" }],
  "forwardedProps": { "source": "form-page" },
  "model": "gptoss-120b"
}
```

```bash
curl -X POST http://localhost:8000/twp/v1/run \
  -H "X-User-Id: user-123" -H "Content-Type: application/json" \
  -d '{"threadId":"t1","runId":"r1","state":{},"messages":[{"id":"m1","role":"user","content":"Fill the description"}],"tools":[],"context":[],"forwardedProps":{}}' \
  --no-buffer
```

**Response:** `text/event-stream`. Each event is a `data: {…}\n\n` line carrying a camelCase JSON payload tagged by `type`. A text answer streams as:

```
data: {"type":"RUN_STARTED","runId":"r1","threadId":"t1"}
data: {"type":"TEXT_MESSAGE_START","messageId":"a1","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"a1","delta":"Done — "}
data: {"type":"TEXT_MESSAGE_END","messageId":"a1"}
data: {"type":"RUN_FINISHED","runId":"r1","threadId":"t1"}
```

When the LLM calls a client-side tool, the run emits the tool-call lifecycle and then finishes — it does **not** synthesize a result or run a confirmation turn:

```
data: {"type":"TOOL_CALL_START","toolCallId":"tc1","toolCallName":"fill_form"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"tc1","delta":"{\"description\":\"...\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"tc1"}
data: {"type":"RUN_FINISHED","runId":"r1","threadId":"t1"}
```

The frontend executes the tool and sends the real result back as a `role="tool"` message in a **continuation run** (same `threadId`); that run preserves the tool-result history into the next LLM turn. Errors surface as `{"type":"RUN_ERROR","message":"...","code":"...","runId":"r1","threadId":"t1"}`.

Event types: `RUN_STARTED` · `TEXT_MESSAGE_START`/`TEXT_MESSAGE_CONTENT`/`TEXT_MESSAGE_END` · `REASONING_START`/`REASONING_MESSAGE_START`/`REASONING_MESSAGE_CONTENT`/`REASONING_MESSAGE_END`/`REASONING_END` · `TOOL_CALL_START`/`TOOL_CALL_ARGS`/`TOOL_CALL_END` · `RUN_FINISHED` · `RUN_ERROR`.

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
| `GET /readyz` | Readiness probe — checks all dependencies (DB, ES, Redis, MinIO); 503 with problem+json on failure. Emits structlog events `probe.ok` (INFO) / `probe.failed` (WARNING, with `error_code`, `detail`, `duration_ms`) per probe. |
| `GET /metrics` | Prometheus metrics (text/plain) |

---

## MCP (Phase 2)

`POST /mcp/v1` — Model Context Protocol server (JSON-RPC 2.0, spec `2025-06-18`; `initialize` echoes a supported older revision — `2025-03-26` / `2024-11-05` — when the client requests one). Exposes the corpus as a single `retrieve` tool. Full spec: [`docs/spec/mcp_server.md`](docs/spec/mcp_server.md).

| Method | Purpose |
|---|---|
| `initialize` | Capability negotiation. |
| `notifications/initialized` | Client signals init complete; server returns 204. |
| `tools/list` | Returns the `retrieve` tool with Pydantic-derived `inputSchema`, hand-authored `outputSchema` (structured source list), and `annotations: {readOnlyHint: true}`. |
| `tools/call` | Invokes `retrieve`. Result `structuredContent.sources` is the machine-readable source list (for the frontend's retrieved-sources panel); `content[0].text` is a `<context>`-wrapped markdown citation table + `### [N]` excerpt blocks for LLM grounding (no internal fields like `document_id`/`score`; cells injection-safe — CR/LF stripped, `\|` escaped; only http(s) `source_url` linkified with markdown-breaking chars percent-encoded; literal `<context>` tags in corpus text neutralised). Unknown args → `-32602 MCP_TOOL_INPUT_INVALID`. |
| `ping` | Returns `{}`. |

Optional `retrieve` arguments (`source_app`, `source_meta`, `min_score`) must be **omitted** to skip filtering — do not send `null`. The `inputSchema` does not advertise `default: null` for these fields; sending explicit `null` returns `-32602`. For `source_app`, use the exact value returned in a prior `retrieve` result's `source_app` metadata field — omit on the first call to search across all sources.

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

---

## Operational Endpoints (`/ops/v1`)

### `POST /ops/v1/retry` — Batch force-retry stuck documents

Immediately re-queues documents in `UPLOADED`, `PENDING`, or `FAILED` states without waiting for the reconciler's 5-minute window. Use `dry_run: true` to preview affected counts before executing.

**Auth:** `x-user-id` header required.

**Request body:**
```json
{
  "statuses": ["FAILED"],          // required; one or more of UPLOADED/PENDING/FAILED
  "dry_run": false,                // true = preview only, no mutations (default false)
  "source_app": "my-app",          // optional — scope to one application
  "source_id": "doc-123",          // optional — scope to one logical document
  "created_after": "2026-06-04T00:00:00Z", // optional — incident window filter
  "limit": 100                     // optional — batch cap 1–500 (default 500)
}
```

**Response `200`:**
```json
{
  "dry_run": false,
  "counts": {
    "FAILED":  {"before": 5, "after": 0},
    "PENDING": {"before": 2, "after": 0}
  },
  "queued": 7,    // documents marked PENDING + enqueued (always 0 when dry_run)
  "skipped": 0    // documents that transitioned between list and mark (always 0 when dry_run)
}
```

**Notes:**
- When `dry_run: true`, `counts.before == counts.after` and `queued == skipped == 0`. `limit` is ignored — `counts.before` reflects the **total** matching rows across the whole DB, letting the operator see full scope before choosing a batch size.
- When `dry_run: false`, documents are processed FIFO (oldest `created_at` first). Each document is atomically claimed via `mark_for_rerun` before enqueueing; documents that transition state between the list scan and the mark are counted as `skipped` (race-safe).
- `limit` caps the number of documents retried in one call; run multiple times to drain a large backlog.

**Notes on `counts`:** all statuses listed in `statuses` always appear as keys, even if their count is 0 in both before and after snapshots.

**Non-2xx cases:**
- `422` — `statuses` missing or empty; `limit` outside 1–500; unrecognised status value; unknown fields in request body (e.g. typo `dryrun` instead of `dry_run`).
