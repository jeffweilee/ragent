# Error Code Catalog (I6)

> Parent: [`docs/00_spec.md §4.1.2`](../00_spec.md#412-error-code-catalog-i6)  
> Rule: every `error_code` added to `src/ragent/errors/codes.py` **MUST** have a row here  
> in the same commit that introduces it.

---

## API-surface error codes

> Codes emitted at the HTTP / JSON-RPC / SSE boundary. Transport varies: most appear in `application/problem+json` bodies; MCP codes appear in JSON-RPC error envelopes; `INGEST_ARCHIVE_UNSAFE` / `INGEST_PDF_TOO_MANY_PAGES` / `INGEST_NOT_FOUND` surface via `documents.error_code` on a `GET /ingest/v1/{id}` poll after async worker rejection.

| `error_code` | HTTP / Surface | When | Origin |
|---|---|---|---|
| `INTERNAL_ERROR`                     | 500         | Global handler fallback — exception carries no `error_code` | Global exception handler |
| `UPSTREAM_ERROR`                     | 502         | Generic upstream service failure (base-class default) | `UpstreamServiceError` base |
| `UPSTREAM_TIMEOUT`                   | 504         | Generic upstream timeout (base-class default) | `UpstreamTimeoutError` base |
| `EMBEDDER_ERROR`                     | 502         | Embedding service raised during an HTTP call | Embedder client |
| `EMBEDDER_TIMEOUT`                   | 504         | Embedding service timed out | Embedder client |
| `LLM_TIMEOUT`                        | 504         | LLM service timed out (pre-stream or mid-stream) | Router T3.10/T3.12 |
| `RERANK_ERROR`                       | 502         | Rerank service raised | Rerank client |
| `RERANK_TIMEOUT`                     | 504         | Rerank service timed out | Rerank client |
| `INGEST_MIME_UNSUPPORTED`            | 415         | MIME outside the §4.2 P1 allow-list | Router T2.13 |
| `INGEST_FILE_TOO_LARGE`              | 413         | Multipart body > 50 MB | Router T2.13 |
| `INGEST_ARCHIVE_UNSAFE`              | 413 via `documents.error_code` | DOCX/PPTX zip preflight rejected the archive — `reason ∈ {invalid, members, ratio, expanded, per_member, traversal}` (T-SEC.3/.4) | Splitter T-SEC.4 |
| `INGEST_PDF_TOO_MANY_PAGES`          | 413 via `documents.error_code` | PDF page count exceeds `INGEST_MAX_PDF_PAGES` (T-SEC.5/.6) | Splitter T-SEC.6 |
| `INGEST_VALIDATION`                  | 422         | Missing/empty `source_id` / `source_app` / `source_title` (S23) | Router T2.13 |
| `INGEST_MINIO_SITE_UNKNOWN`          | 422         | `minio_site` not in `MinioSiteRegistry` | Router T2.13 |
| `INGEST_OBJECT_NOT_FOUND`            | 422         | `(minio_site, object_key)` HEAD-probe miss | Router T2.13 |
| `INGEST_NOT_FOUND`                   | 404         | `GET/DELETE /ingest/v1/{id}` or `POST /ingest/v1/{id}/rerun` on unknown id | Service T2.10 |
| `INGEST_NOT_RERUNNABLE`              | 409         | `/rerun` on a document in `READY` or `DELETING` state | Router (rerun endpoint) |
| `MISSING_USER_ID`                    | 422         | User-id header absent or empty after JWT verification | Identity middleware |
| `CHAT_RATE_LIMITED`                  | 429 + `Retry-After` | Per-user fixed-window quota exceeded on `/chat/v1[/stream]` (B31, S37) | Router-level Depends T3.16 |
| `CHATAGENT_INVALID_RESUME`           | SSE-error only | `/chatagent/v3` resume carries >1 `resolved` interrupt — upstream takes a single `lastMessageId` (emitted as `RUN_ERROR` over a 200 stream) | `ADKCaller` resume validation |
| `EMBEDDING_LIFECYCLE_INVALID_STATE`  | 409         | Embedding model state-machine transition rejected (B50) | Embedding lifecycle router |
| `EMBEDDING_CUTOVER_PREFLIGHT_FAILED` | 409         | Cutover preflight (warmup / similarity gate) failed (B50) | Embedding lifecycle router |
| `EMBEDDING_INVALID_CONFIG`           | 422         | Invalid promote payload (B50) | Embedding lifecycle router |
| `EMBEDDING_FIELD_NAME_COLLISION`     | 422         | Field name collision with a still-mapped retired field (B50) | Embedding lifecycle router |
| `EMBEDDING_REGISTRY_NOT_READY`       | 503         | Embedding registry not ready for queries | Embedding lifecycle router |
| `FEEDBACK_TOKEN_INVALID`             | 401         | HMAC mismatch, malformed token, or `shown_source_ids` mismatch (T-FB.6, B55) | Router (feedback) |
| `FEEDBACK_TOKEN_EXPIRED`             | 410         | Token `ts` outside the 7-day window (T-FB.6, B55) | Router (feedback) |
| `FEEDBACK_SOURCE_INVALID`            | 422         | `source_id ∉ shown_source_ids` (T-FB.6) | Router (feedback) |
| `FEEDBACK_VALIDATION`                | 422         | vote ∉ {±1}, reason outside B56 enum, missing required field | Schema (feedback) |
| `LLM_STREAM_INTERRUPTED`             | SSE-error only | LLM SSE stream closed before `[DONE]` after at least one delta yielded (B6, T-CHAOS.C5) | Router T3.12 |
| `LLM_ERROR`                          | 502 / SSE-error | Pre-stream LLM failure (problem+json) or mid-stream retries exhausted (B6) | Router T3.10/T3.12 |
| `MCP_PARSE_ERROR`                    | JSON-RPC `-32700` | Request body is not valid JSON (S64) | Router P2.5 |
| `MCP_INVALID_REQUEST`                | JSON-RPC `-32600` | Missing `jsonrpc:"2.0"` / `method`; malformed envelope | Router P2.5 |
| `MCP_METHOD_NOT_FOUND`               | JSON-RPC `-32601` | Method outside §3.8.2 allow-list (S61) | Router P2.5 |
| `MCP_TOOL_NOT_FOUND`                 | JSON-RPC `-32602` | `tools/call` with unknown `name` (S62) | Router P2.5 |
| `MCP_TOOL_INPUT_INVALID`             | JSON-RPC `-32602` | `tools/call` arguments fail `inputSchema` validation (S63) | Router P2.5 |
| `MCP_TOOL_EXECUTION_FAILED`          | JSON-RPC `-32001` | Underlying retrieval pipeline raises (S67) | Router P2.5 |
| `ES_PLUGIN_MISSING`                  | 503 (`/readyz`) | ES cluster missing `analysis-icu` plugin (B26, T0.8g) | Bootstrap / readyz |
| `ES_INDEX_MISSING`                   | 503 (`/readyz`) | A `resources/es/*.json` index is absent at boot | Bootstrap / readyz |
| `SCHEMA_DRIFT`                       | 503 (`/readyz`) + log `event=schema.drift` | Live schema differs from `schema.sql` / `resources/es/` | Bootstrap |
| `PROBE_TIMEOUT`                      | 503 (`/readyz`) | Per-component probe timed out | Bootstrap / readyz |
| `DEPENDENCY_DOWN`                    | 503 (`/readyz`) | A required dependency is unreachable | Bootstrap / readyz |
| `METRICS_DB_UNAVAILABLE`             | 503 (`/readyz`) | Metrics DB unavailable | Bootstrap / readyz |
| `AUTH_TOKEN_EXPIRED`                 | 401             | JWT `exp` claim is in the past (T8.1a) | Auth middleware T8.2a |
| `AUTH_CLAIM_MISSING`                 | 401             | `<RAGENT_JWT_CLAIM_USER_ID>` claim absent or empty (T8.1a) | Auth middleware T8.2a |
| `AUTH_TOKEN_INVALID`                 | 401             | JWT absent, malformed, bad signature, wrong `iss`/`aud`, or any other JWKS failure (T8.1a) | Auth middleware T8.2a |

---

## TaskErrorCode — async pipeline errors (persisted on `documents.error_code`)

| `error_code` | When | Origin |
|---|---|---|
| `PIPELINE_TIMEOUT_AGGREGATE`     | 300 s wall-clock exceeded; polled via `GET /ingest/v1/{id}` (B18, S34) | Worker T3.2j |
| `PIPELINE_UNROUTABLE`            | MIME → splitter has no registered route | Worker pipeline |
| `CHUNK_BUDGET_EXCEEDED`          | `CHUNK_MAX_PIECES_PER_ATOM` exceeded during chunking | `_BudgetChunker` |
| `ES_WRITE_ERROR`                 | `DocumentWriter` raised during ES bulk write | Worker pipeline |
| `EMBEDDER_ERROR`                 | Embedder client raised inside the pipeline step | Worker pipeline |
| `PIPELINE_UNEXPECTED_ERROR`      | Catch-all for uncategorised pipeline step exceptions | Worker pipeline |
| `PIPELINE_MAX_ATTEMPTS_EXCEEDED` | Reconciler swept a stuck-PENDING document past max retries | Reconciler |

---

## Operational signals (not `error_code` values — never in API responses)

| Signal | Surface | When |
|---|---|---|
| `es.bbq_unsupported` | structured log | Cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW (B26) |
| `reconciler_tick_total` stale | Prometheus alert `ReconcilerTickStalled` | Flat > 10 min (R8, S30) |
| Ingest failure rate > 10 % | Prometheus alert `IngestHighFailureRate` | `ragent_pipeline_runs_total{outcome="failed"}` rate/total > 0.10 for 2 min (P2.1) |
| Reranker degraded > 5 min | Prometheus alert `RerankerDegradedPersistent` | `rerank_degraded_total` rate (2 m window) > 0 for 5 min (P2.3) |
| Worker pipeline p99 > 5 min | Prometheus alert `WorkerPipelineSlow` | `worker_pipeline_duration_seconds` p99 > 300 s for 5 min |
| `/readyz` probe stuck failing | Prometheus alert `ReadyzProbeFailing` (critical) | `ragent_readyz_probe_status == 0` for 2 min |

> **Chat 422 without named `error_code`:** `messages` absent/empty, `provider` not in allow-list, and `source_app`/`source_meta` filter violations are rejected by Pydantic and return a standard 422 `problem+json` with `errors[]` — no named `error_code`.
