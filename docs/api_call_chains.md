# API Call Chains — ragent

> Authored: 2026-05-27  
> Maintained by: Dev / SRE  
> Source modules: `src/ragent/routers/`, `src/ragent/clients/`, `src/ragent/workers/`

This document maps every external-facing API surface to the full chain of upstream
calls it makes, and annotates each chain with:

- **Upstream services touched** (with retry policy)
- **Exception handling** at each hop (caught vs. propagated)
- **Process-exit conditions** (exceptions that abort the process, not just the request)
- **Degradation surface** (where an upstream failure produces a degraded-but-live response)

---

## Table of Contents

1. [Startup (lifespan) — boot-abort conditions](#1-startup-lifespan--boot-abort-conditions)
2. [POST /ingest/v1 — create ingest](#2-post-ingestv1--create-ingest)
3. [GET /ingest/v1/{id} — get document](#3-get-ingestv1id--get-document)
4. [GET /ingest/v1 — list documents](#4-get-ingestv1--list-documents)
5. [DELETE /ingest/v1/{id} — delete document](#5-delete-ingestv1id--delete-document)
6. [POST /ingest/v1/{id}/rerun — manual rerun](#6-post-ingestv1idrerun--manual-rerun)
7. [POST /chat/v1 — synchronous chat](#7-post-chatv1--synchronous-chat)
8. [POST /chat/v1/stream — streaming chat](#8-post-chatv1stream--streaming-chat)
9. [POST /retrieve/v1 — standalone retrieval](#9-post-retrievev1--standalone-retrieval)
10. [POST /mcp/v1 (tools/call retrieve) — MCP retrieve](#10-post-mcpv1-toolscall-retrieve--mcp-retrieve)
11. [POST /feedback/v1 — feedback submission](#11-post-feedbackv1--feedback-submission)
12. [Worker: ingest.pipeline task](#12-worker-ingestpipeline-task)
13. [Reconciler: ingest.supersede + stuck-PENDING sweep](#13-reconciler-ingestsupersede--stuck-pending-sweep)
14. [Token exchange — cross-cutting concern](#14-token-exchange--cross-cutting-concern)
15. [Summary: exception surface map](#15-summary-exception-surface-map)

---

## 1. Startup (lifespan) — boot-abort conditions

Called from `bootstrap/app.py::create_app()` → `lifespan()` → `_check_infra_ready()`.

```
lifespan()
  ├── broker.startup()          — TaskIQ Redis Sentinel connection
  ├── init_schema()             — alembic head check (MariaDB)
  ├── _check_infra_ready()
  │     ├── probe_mariadb()     — SELECT 1 on MariaDB engine
  │     ├── probe_es()          — ES cluster health ping
  │     ├── broker.find_task()  — assert "ingest.pipeline" + "ingest.supersede" registered
  │     └── tm.get_token()      — Auth API token exchange (all token_managers)
  └── embedding_registry.refresh()   — MariaDB read for active model (non-fatal)
```

### Exceptions → process exit (boot abort)

| Condition | Exception raised | Outcome |
|---|---|---|
| MariaDB unreachable | `RuntimeError("infra not ready: mariadb …")` | uvicorn/k8s restarts pod |
| ES unreachable | `RuntimeError("infra not ready: es …")` | pod restart |
| TaskIQ task label missing | `RuntimeError("infra not ready: TaskIQ task not registered")` | pod restart |
| Auth API 503 / timeout at boot | `RuntimeError("Token refresh failed")` → re-raised as `RuntimeError("infra not ready: token exchange failed …")` | pod restart |
| K8s SA token file missing | `RuntimeError("Failed to read Kubernetes service account token")` → same re-raise chain | pod restart |
| `RETRIEVAL_TOP_K` env out of `[1, 200]` | `RuntimeError` at **module import** of `pipelines/retrieve.py` | pod restart at import time |

> **Design intent**: All these are fail-fast. An operator who misconfigures the token
> exchange URL or has a stale J1 token discovers it at deploy time, not at first-request.

### Non-fatal startup steps

- `embedding_registry.refresh()` — failures degrade to a stale-warning log; boot continues.
  First ingest/chat after boot will re-attempt the refresh per-task/per-request.

---

## 2. POST /ingest/v1 — create ingest

```
HTTP request
  └── Middleware: user-id check (trust header | JWT verify)
  └── IngestRouter.create_document()
        └── IngestService.create()
              ├── [inline/upload path] encode → MariaDB insert → broker.kiq("ingest.pipeline")
              └── [file path] validate minio_site, head_object (MinIO) → MariaDB insert → kiq
```

**Upstream calls**: MariaDB (insert row), TaskIQ broker (enqueue), optional MinIO HEAD.

**Exception handling**:

| Exception | Caught at | Response |
|---|---|---|
| `MimeNotAllowed` | Router | 415 `INGEST_MIME_UNSUPPORTED` |
| `FileTooLarge` | Router | 413 `INGEST_FILE_TOO_LARGE` |
| `UnknownMinioSiteError` | Router | 422 `INGEST_MINIO_SITE_UNKNOWN` |
| `ObjectNotFoundError` | Router | 422 `INGEST_OBJECT_NOT_FOUND` |
| `RequestValidationError` | `_IngestRoute` wrapper | 415 or 422 |
| `aiomysql.OperationalError` (MariaDB down) | Global handler | 500 `INTERNAL_ERROR` |
| `redis.ConnectionError` (broker enqueue) | Global handler | 500 `INTERNAL_ERROR` |

**Process-exit risk**: None at request time.

---

## 3. GET /ingest/v1/{id} — get document

```
HTTP request
  └── IngestRouter.get_document()
        └── IngestService.get()
              └── DocumentRepository.get_document_by_id()   [MariaDB SELECT]
```

**Exception handling**:

| Exception | Response |
|---|---|
| `doc is None` | 404 `INGEST_NOT_FOUND` |
| `aiomysql.OperationalError` | 500 `INTERNAL_ERROR` (global handler) |

---

## 4. GET /ingest/v1 — list documents

```
HTTP request → IngestService.list() → MariaDB SELECT (paginated)
```

| Exception | Response |
|---|---|
| `aiomysql.OperationalError` | 500 `INTERNAL_ERROR` |

---

## 5. DELETE /ingest/v1/{id} — delete document

```
HTTP request → IngestService.delete() → MariaDB UPDATE (status=DELETING)
```

Silent 204 even when document_id not found (idempotent DELETE).

---

## 6. POST /ingest/v1/{id}/rerun — manual rerun

```
HTTP request → IngestService.rerun()
  ├── DocumentRepository.get_for_rerun()   [MariaDB]
  ├── DocumentRepository.reset_for_rerun() [MariaDB UPDATE status=PENDING]
  └── broker.kiq("ingest.pipeline")
```

| Exception | Response |
|---|---|
| `DocumentNotFound` | 404 `INGEST_NOT_FOUND` |
| `DocumentNotRerunnable` (status READY/DELETING) | 409 `INGEST_NOT_RERUNNABLE` |
| MariaDB down | 500 `INTERNAL_ERROR` |

---

## 7. POST /chat/v1 — synchronous chat

Full call chain (all upstream services):

```
POST /chat/v1
  └── Middleware: user-id / JWT
  └── ChatRouter.chat()
        ├── RateLimiter.check()              [Redis: INCR + EXPIRE NX]
        ├── _detect_intent()                 [LLM API — lightweight call]
        │     └── LLMClient.chat()
        │           ├── TokenManager.get_token() → Auth API
        │           └── POST {LLM_API_URL}   (retry 3×@2s)
        ├── run_retrieval() [conditional — skipped for GREETING/CHITCHAT or context_mode=caller]
        │     └── Haystack pipeline.run()
        │           ├── _QueryEmbedder.run()
        │           │     └── EmbeddingClient.embed()
        │           │           ├── TokenManager.get_token() → Auth API
        │           │           └── POST {EMBEDDING_API_URL}  (retry 3×@1s)
        │           ├── ElasticsearchBM25Retriever.run()   [ES query]
        │           ├── ElasticsearchEmbeddingRetriever / _DynamicFieldEmbeddingRetriever [ES kNN]
        │           ├── _FeedbackMemoryRetriever.run() [optional — ES kNN + MariaDB]
        │           ├── DocumentJoiner.run()
        │           ├── _Reranker.run() [optional]
        │           │     └── RerankClient.rerank()
        │           │           ├── TokenManager.get_token() → Auth API
        │           │           └── POST {RERANK_API_URL}    (retry 3×@2s)
        │           ├── _SourceHydrator.run()               [MariaDB get_sources_by_document_ids]
        │           └── _ExcerptTruncator.run()
        └── LLMClient.chat()                 [main answer generation]
              ├── TokenManager.get_token() → Auth API
              └── POST {LLM_API_URL}         (retry 3×@2s)
```

**Exception handling**:

| Exception | Caught at | Response |
|---|---|---|
| Rate limit exceeded (Redis ok) | Router | 429 `CHAT_RATE_LIMITED` + `Retry-After` |
| `redis.ConnectionError` (rate limiter) | Global handler | 500 `INTERNAL_ERROR` — **Redis down = all chat requests fail** |
| `UpstreamServiceError` (embedding 503) | Global handler | 502 `EMBEDDER_ERROR` |
| `UpstreamTimeoutError` (embedding timeout) | Global handler | 504 `EMBEDDER_TIMEOUT` |
| `UpstreamServiceError` (rerank 5xx) | `_Reranker.run()` | **Fail-open**: returns RRF-ordered docs[:top_k], increments `rerank_degraded_total` |
| `UpstreamServiceError` (rerank 4xx, e.g. 401) | `_Reranker.run()` re-raises | 502 `RERANK_ERROR` |
| `UpstreamServiceError` (LLM intent call 503) | `_detect_intent()` catches ALL | Falls back to `QUESTION` intent (fail-safe) |
| `UpstreamServiceError` (LLM answer 503) | Global handler | 502 `LLM_ERROR` |
| `UpstreamTimeoutError` (LLM answer timeout) | Global handler | 504 `LLM_TIMEOUT` |
| ES `ConnectionError` | Global handler | 500 `INTERNAL_ERROR` |
| `aiomysql.OperationalError` (SourceHydrator) | Global handler | 500 `INTERNAL_ERROR` |

**Degradation behaviours**:
- Reranker 5xx → fail-open (ranking degrades gracefully, retrieval continues)
- Intent-detection LLM failure → fall back to `QUESTION` (retrieval still runs)
- `_detect_intent()` uses `temperature=0, max_tokens=10` — a separate, cheaper LLM call before the main answer call

---

## 8. POST /chat/v1/stream — streaming chat

Same upstream chain as synchronous chat up through retrieval. Differences:

```
...same retrieval chain...
  └── LLMClient.stream()           [SSE streaming]
        ├── TokenManager.get_token() → Auth API
        └── POST {LLM_API_URL} (retry 3×@2s, except LLMStreamInterruptedError)
              └── iter_lines() → yield delta SSE frames
```

**Exception handling in the streaming generator** (`_generate()`):

| Exception | Behaviour |
|---|---|
| `LLMStreamInterruptedError` (stream closed before `[DONE]`) | Emits `data: {"type":"error","error_code":"LLM_STREAM_INTERRUPTED"}` SSE frame; **never retried** (partial content already sent) |
| Any other `UpstreamServiceError` / `UpstreamTimeoutError` | Emits `data: {"type":"error","error_code":"<code>"}` SSE frame |
| Pre-stream errors (rate-limit, retrieval) | Returns non-streaming `Response` (same as sync chat) before generator starts |

> **Key**: Once the `StreamingResponse` generator has started, all LLM exceptions are
> converted to SSE error frames — the HTTP status is already 200 at that point.

---

## 9. POST /retrieve/v1 — standalone retrieval

```
POST /retrieve/v1
  └── Middleware: user-id / JWT
  └── RetrieveRouter.retrieve()
        └── run_retrieval()   [same Haystack pipeline as chat retrieval]
              ├── EmbeddingClient.embed()   → Auth API + Embedding API
              ├── ES BM25 + kNN retrieval
              ├── _FeedbackMemoryRetriever  [optional: ES + MariaDB]
              ├── _Reranker                 [optional: Rerank API — fail-open on 5xx]
              ├── _SourceHydrator           [MariaDB]
              └── _ExcerptTruncator
```

**Exception handling**: identical to chat retrieval chain (§7) — no LLM call, no rate limit.

---

## 10. POST /mcp/v1 (tools/call retrieve) — MCP retrieve

JSON-RPC 2.0 envelope → `tools/call` → `_handle_tools_call()` → `run_retrieval()`

```
POST /mcp/v1
  └── McpRouter.mcp_jsonrpc()
        ├── JSON parse / schema validate
        ├── tools/list   → no upstream calls
        │     returns _RETRIEVE_TOOL_SCHEMA including:
        │       annotations.readOnlyHint=true   (MCP 2025-03-26+; older clients ignore)
        ├── initialize   → no upstream calls
        └── tools/call retrieve
              └── run_retrieval()   [same Haystack pipeline — §9]
                    └── doc_to_source_entry(d, max_chars=excerpt_max_chars)
                          excerpt_max_chars bound at router-creation from EXCERPT_MAX_CHARS env
                          (same value as POST /retrieve/v1 — they share the container config)
```

**Exception handling**:

| Exception | Response |
|---|---|
| JSON parse error | JSON-RPC `-32700` + `MCP_PARSE_ERROR` |
| Invalid request shape | JSON-RPC `-32600` + `MCP_INVALID_REQUEST` |
| Unknown method | JSON-RPC `-32601` + `MCP_METHOD_NOT_FOUND` |
| Input schema violation (jsonschema) | JSON-RPC `-32602` + `MCP_TOOL_INPUT_INVALID` |
| Unknown tool name | JSON-RPC `-32602` + `MCP_TOOL_NOT_FOUND` |
| Any exception from `run_retrieval()` | JSON-RPC `-32001` + `MCP_TOOL_EXECUTION_FAILED` — **all upstream failures are wrapped as tool errors**; HTTP status stays 200 per JSON-RPC spec |
| Body > `MCP_REQUEST_MAX_BYTES` (default 256 KiB) | HTTP 413 `MCP_INVALID_REQUEST` (problem+json, not JSON-RPC) |

> **MCP specificity**: `run_retrieval` failures (embedding down, ES down, etc.)
> surface as `{"isError": true}` tool result or as a JSON-RPC `-32001` error envelope,
> **not** as HTTP 5xx. MCP clients must inspect the JSON-RPC layer, not HTTP status.

---

## 11. POST /feedback/v1 — feedback submission

```
POST /feedback/v1
  └── FeedbackRouter
        ├── HMAC token verify                   [no upstream — pure compute]
        ├── EmbeddingClient.embed(query)         → Auth API + Embedding API
        └── ES index feedback document           → ES bulk
```

| Exception | Response |
|---|---|
| HMAC invalid | 401 `FEEDBACK_TOKEN_INVALID` |
| HMAC expired | 410 `FEEDBACK_TOKEN_EXPIRED` |
| Source pair not in shown_sources | 422 `FEEDBACK_SOURCE_INVALID` |
| `UpstreamServiceError` (embedding) | 502 `EMBEDDER_ERROR` |
| ES write error | 500 `INTERNAL_ERROR` (global handler) |

---

## 12. Worker: ingest.pipeline task

Runs inside `ragent.worker` process (TaskIQ). No HTTP response — terminal outcome written to
MariaDB `documents.status` + `error_code` + `error_reason`.

```
ingest_pipeline_task(document_id)
  ├── embedding_registry.refresh()          [MariaDB — non-fatal, stale-warning on fail]
  ├── repo.claim_for_processing()           [MariaDB atomic UPDATE PENDING←UPLOADED|PENDING]
  ├── minio_registry.head_object()          [MinIO HEAD]
  ├── minio_registry.get_object()           [MinIO GET]   (retry 3× on transient errors)
  ├── [optional] UnprotectClient.unprotect() [Unprotect API]
  │     └── raise_for_status()             — **caught by bare `except Exception:` → fail-open**
  │         The unprotect failure is logged as WARNING; the original bytes are used instead.
  ├── container.ingest_pipeline.run()       [Haystack sync pipeline]
  │     ├── Loader → parse bytes
  │     ├── Splitter → chunk text
  │     ├── Chunker → DocumentSplitter
  │     ├── EmbeddingClient._call()         → Auth API + Embedding API  (retry 3×@1s)
  │     └── DocumentWriter → ES bulk index
  ├── asyncio.wait_for(…, timeout=300s)     [aggregate wall-clock cap]
  └── repo.promote_to_ready_and_demote_siblings()  [MariaDB UPDATE]
```

**Exception handling** (inside worker body — no HTTP response path):

| Exception | MariaDB status written | Worker continues? |
|---|---|---|
| `asyncio.TimeoutError` (>300s) | `FAILED` / `PIPELINE_TIMEOUT_AGGREGATE` | yes (task returns) |
| `UpstreamServiceError` (embedding down) | `FAILED` / `EMBEDDER_ERROR` | yes |
| `UpstreamTimeoutError` (embedding timeout) | `FAILED` / `EMBEDDER_TIMEOUT` | yes |
| ES write error | `FAILED` / `ES_WRITE_ERROR` | yes |
| `ArchiveBombError` / `PdfTooManyPagesError` | `FAILED` / error_code from exception | yes |
| Any other `Exception` | `FAILED` / `PIPELINE_UNEXPECTED_ERROR` | yes |
| `repo.claim_for_processing()` returns `None` | no status write (row already terminal) | yes |

**Process-exit risk**: **None**. The worker's top-level `try/except` in `ingest_pipeline_task`
catches everything and writes a terminal status. TaskIQ's own task error handler catches
anything that escapes.

**Unprotect API failure**: Fail-open — the original (protected) bytes are used. The downstream
parser may then fail (e.g. ZIP-in-ZIP bomb guard fires) which writes `FAILED`.

---

## 13. Reconciler: ingest.supersede + stuck-PENDING sweep

```
reconciler.py (K8s CronJob, or in-process background)
  ├── repo.find_stuck_pending()    [MariaDB SELECT — find PENDING rows older than N minutes]
  ├── broker.kiq("ingest.pipeline", document_id)  [re-dispatch via TaskIQ]
  └── repo.mark_max_attempts_exceeded()            [MariaDB UPDATE → FAILED]
```

No external AI API calls. Only MariaDB + TaskIQ broker.  
Exception handling: each tick is wrapped; a MariaDB failure logs `reconciler.tick.failed` and
the cron retries on the next scheduled interval.

---

## 14. Token exchange — cross-cutting concern

Every AI client (`EmbeddingClient`, `LLMClient`, `RerankClient`) calls
`TokenManager.get_token()` before each upstream HTTP request.

```
TokenManager.get_token()   [protected by threading.Lock]
  └── if cached token not expired → return cached
  └── _refresh()
        ├── _get_j1()      → read J1 from env var OR K8s SA token file
        └── self._http.post(auth_url, json={"key": j1})
              ├── re-raise httpx.TimeoutException  (preserves type for classify_upstream_error)
              └── raise RuntimeError("Token refresh failed")  on all other exceptions
```

### Exception transparency

`_refresh()` re-raises `httpx.TimeoutException` unwrapped; all other exceptions are wrapped
in `RuntimeError("Token refresh failed")`.

**Effect at request time**:  
A token exchange **timeout** propagates as `httpx.TimeoutException` → `classify_upstream_error`
returns `UpstreamTimeoutError` (HTTP 504). Non-timeout failures (503, connection refused)
produce `RuntimeError` → `UpstreamServiceError` (HTTP 502).

**Effect at boot time**:  
`_check_infra_ready()` catches any exception from `get_token()` (both `httpx.TimeoutException`
and `RuntimeError`) and re-wraps as `RuntimeError("infra not ready: token exchange failed …")`
→ **boot abort** (intended behavior — unchanged).

### Retry behaviour

The client retry loops (`for attempt in range(3)`) call `get_token()` on each attempt. So
a token exchange outage causes:
- Up to 3 token refresh attempts per embedding/LLM/rerank call
- After 3 failures → `UpstreamServiceError` (502) returned to caller
- All concurrent requests needing a new token serialise through the `threading.Lock`
  (single-flight refresh), preventing a thundering herd against the auth API

---

## 15. Summary: exception surface map

### API-surface exceptions (process stays alive)

| Upstream failure | Caught at | HTTP status | `error_code` |
|---|---|---|---|
| Token exchange 503 (runtime) | Client retry loop (3×) | 502 | `EMBEDDER_ERROR` / `LLM_ERROR` / `RERANK_ERROR` |
| Token exchange timeout (runtime) | Client retry loop (3×) | 504 | `EMBEDDER_TIMEOUT` / `LLM_TIMEOUT` / `RERANK_TIMEOUT` |
| Embedding API 503 | Client retry loop (3×@1s) | 502 | `EMBEDDER_ERROR` |
| Embedding API timeout | Client retry loop (3×@1s) | 504 | `EMBEDDER_TIMEOUT` |
| LLM API 503 (answer) | Client retry loop (3×@2s) | 502 | `LLM_ERROR` |
| LLM API 503 (intent detection) | `_detect_intent()` bare `except` | — | falls back to `QUESTION` intent |
| LLM stream interrupted | `_generate()` handler | SSE error frame | `LLM_STREAM_INTERRUPTED` |
| Rerank API 5xx | `_Reranker.run()` | — | **fail-open** (degraded ranking) |
| Rerank API 4xx (auth/config) | `_Reranker.run()` re-raises | 502 | `RERANK_ERROR` |
| ES unreachable (runtime) | Global handler | 500 | `INTERNAL_ERROR` |
| MariaDB unreachable (runtime) | Global handler | 500 | `INTERNAL_ERROR` |
| Redis down (rate limiter) | Global handler | 500 | `INTERNAL_ERROR` — chat entirely blocked |
| MinIO transient 503 (worker) | `minio.get_object` retry (3×) | writes `FAILED` | task-level |
| Unprotect API failure (worker) | Bare `except Exception` in worker | — | **fail-open** (original bytes used) |

### Process-exit conditions (boot-abort only)

| Condition | Module | How to fix |
|---|---|---|
| MariaDB unreachable at boot | `bootstrap/app.py` | Fix DSN / wait for DB |
| ES unreachable at boot | `bootstrap/app.py` | Fix ES URL / wait for ES |
| Token exchange 503 at boot | `clients/auth.py` + `bootstrap/app.py` | Fix `AI_API_AUTH_URL` / J1 token |
| K8s SA token file missing | `clients/auth.py` | Mount the service account volume |
| `RETRIEVAL_TOP_K` outside `[1, 200]` | `pipelines/retrieve.py` | Fix env var |
| TaskIQ task label not registered | `bootstrap/app.py` | Ensure worker modules imported before `lifespan` |

> **No runtime exception causes a process exit** — FastAPI's global exception handler
> (`_register_unhandled_exception_handler`) catches everything that escapes a route
> handler and returns a structured problem+json response.
