### 4.6 Environment Variables (C2 + B28)

> **Inventory rules (B28):** every external dependency, every per-call timeout, every operational threshold, and every credential MUST appear in this table. Code that reads a literal value not represented here is a spec drift bug. Vars marked `(required)` have no default and refuse boot.

> **v2 removed vars (C6):** `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/SECURE/BUCKET` (→ `MINIO_SITES`), `INGEST_MAX_FILE_SIZE_BYTES` (→ `INGEST_INLINE/FILE_MAX_BYTES`), `CHUNK_TARGET_CHARS_EN/CJK/CSV`, `CHUNK_OVERLAP_CHARS_EN/CJK/CSV`, `CHUNK_HARD_SPLIT_OVERLAP_CHARS`.

#### 4.6.1 Bootstrap & HTTP server

| Variable | Default | Description |
|---|---|---|
| `RAGENT_ENV`                          | (required)       | `dev` \| `staging` \| `prod`. Modes `none`/`user_header`/`jwt_prefer_header` require `dev`; `jwt_header` tolerates any value (§1, §3.5). |
| `RAGENT_AUTH_MODE`                    | `user_header`    | `none` \| `user_header` \| `jwt_header` \| `jwt_prefer_header`. `none`: inject `"anonymous"`, no header needed (dev only). `user_header`: trust `<RAGENT_USER_ID_HEADER>` directly (dev only). `jwt_header`: OIDC JWT only. `jwt_prefer_header`: JWT wins when present, fallback to header (dev only). |
| `RAGENT_USER_ID_HEADER`               | `X-User-Id`      | Canonical header name carrying the downstream `user_id`. In `user_header`/`jwt_prefer_header` mode this is the inbound header read directly; in JWT modes the extracted claim is injected into this header on the request scope. `RequestLoggingMiddleware` reads `request.scope["ragent.user_id"]` — not the header name — so customising this does not break `api.request` logging. |
| `RAGENT_JWT_HEADER`                   | `X-Auth-Token`   | **`jwt_header`/`jwt_prefer_header` only.** Inbound header carrying the raw JWT (no `Bearer ` prefix). |
| `RAGENT_JWT_CLAIM_USER_ID`            | `preferred_username` | **`jwt_header`/`jwt_prefer_header` only.** JWT payload claim path used as the downstream `user_id`. Verified value is non-empty string; missing/empty → 401 `AUTH_CLAIM_MISSING`. |
| `OIDC_DOMAIN`                         | (required for JWT modes) | OIDC issuer domain. JWKS is fetched from `{scheme}://<OIDC_DOMAIN>/.well-known/jwks.json`; verifier validates `iss == discovery["issuer"]`. Guard exits if unset for `jwt_header`/`jwt_prefer_header`. |
| `OIDC_AUDIENCE`                       | (required for JWT modes) | Expected `aud` claim. Tokens with mismatched `aud` → 401 `AUTH_TOKEN_INVALID`. |
| `OIDC_USE_HTTPS`                      | `true`           | Scheme toggle for the OIDC discovery + JWKS URL. Set `false` ONLY for in-cluster discovery or local fixture; production deployments MUST keep `true`. |
| `OIDC_VERIFY_SSL`                     | `true`           | Verify the IdP's TLS certificate during OIDC discovery + JWKS fetch. Set `false` ONLY for dev/staging against self-signed Keycloak. For production with a private CA, leave `true` and mount the CA via `SSL_CERT_FILE` instead. |
| `RAGENT_JWT_VERIFY_AUD`               | `true`           | **`jwt_header`/`jwt_prefer_header` only.** When `false`, audience claim check is skipped. Guard requires `RAGENT_ENV=dev`. |
| `RAGENT_JWT_VERIFY_EXP`               | `true`           | **`jwt_header`/`jwt_prefer_header` only.** When `false`, expiry claim check is skipped. Guard requires `RAGENT_ENV=dev`. |
| `RAGENT_PERMISSION_INGEST_ENABLED`    | `false`          | **P2 only.** When `true`, `GET/DELETE /ingest/v1/{id}` and `GET /ingest/v1` enforce `PermissionClient` (§3.5). Default off — gate is wired but inert until OpenFGA tuples exist. |
| `RAGENT_PERMISSION_CHAT_ENABLED`      | `false`          | **P2 only.** When `true`, chat retrieval applies the `PermissionClient` post-filter (§3.5). Default off. |
| `RAGENT_HOST`                         | `127.0.0.1`      | API bind address. Consumed by the legacy `python -m ragent.api` shim; primary startup `uvicorn ragent.bootstrap.app:create_app --factory` passes `--host` directly. |
| `RAGENT_PORT`                         | `8000`           | API bind port. Consumed by the legacy `python -m ragent.api` shim; primary startup passes `--port` to uvicorn directly. |
| `LOG_LEVEL`                           | `INFO`           | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. Applies to app + TaskIQ + Reconciler. |
| `CORS_ALLOW_ORIGINS`                  | *(unset)*        | Comma-separated list of allowed CORS origins (e.g. `https://app.example.com,https://admin.example.com`). When unset or empty, no `CORSMiddleware` is added and all cross-origin requests are denied. |

#### 4.6.2 Datastore connections (boot-blocking)

| Variable | Default | Description |
|---|---|---|
| `MARIADB_DSN`                         | (required)       | Full SQLAlchemy DSN, e.g. `mysql+aiomysql://@host:3306/ragent?charset=utf8mb4`. Used by repositories, bootstrap, `/readyz`. |
| `MARIADB_POOL_RECYCLE_SECONDS`        | `280`            | SQLAlchemy `pool_recycle` value. Connections older than this are discarded on checkout. Must be less than the server-side `wait_timeout`; default 280 s assumes a 300 s server timeout. |
| `ES_HOSTS`                            | (required)       | Comma-separated `https?://host:port` list. |
| `ES_USERNAME`                         | (optional)       | Basic-auth username; omit for unauthenticated dev clusters. |
| `ES_PASSWORD`                         | (optional)       | Basic-auth password. |
| `ES_API_KEY`                          | (optional)       | Alternative to user/password (mutually exclusive). |
| `ES_VERIFY_CERTS`                     | `true`           | Set `false` for self-signed dev clusters. |
| `ES_CHUNKS_INDEX`                     | `chunks_v1`      | Chunks index name. Threaded through `Container.chunks_index_name` to `ElasticsearchDocumentStore`, `_FeedbackMemoryRetriever`, `VectorExtractor`, `Reconciler`, and `/readyz` ES probe (T-EI.1). `init_es` also honours it when PUT-ing the `chunks_v1.json` schema, so override-and-rename works end-to-end (T-EI.6 / B60). Non-chunks resources (e.g. `feedback_v1.json`) keep filename-as-name semantics. |
| `MINIO_SITES`                         | (required)       | v2: JSON list of `{name, endpoint, access_key, secret_key, bucket, secure?, read_only?}`. Must include `name="__default__"` (inline ingest). Supersedes the five legacy vars below. |
| `MINIO_ENDPOINT`                      | (optional)       | DEPRECATED. |
| `MINIO_ACCESS_KEY`                    | (optional)       | DEPRECATED. |
| `MINIO_SECRET_KEY`                    | (optional)       | DEPRECATED. |
| `MINIO_SECURE`                        | `false`          | DEPRECATED. |
| `MINIO_BUCKET`                        | `ragent`         | DEPRECATED. |

#### 4.6.3 Redis (B27)

| Variable | Default | Description |
|---|---|---|
| `REDIS_MODE`                          | `standalone`     | `standalone` \| `sentinel`. Applies to broker and rate-limiter. |
| `REDIS_BROKER_URL`                    | `redis://localhost:6379/0` | TaskIQ broker URL (mode=standalone). |
| `REDIS_RATELIMIT_URL`                 | `redis://localhost:6379/1` | Rate-limiter URL (mode=standalone). |
| `REDIS_SENTINEL_HOSTS`                | (required if mode=sentinel) | Comma-separated `host:port` list (≥ 3 nodes recommended). |
| `REDIS_BROKER_SENTINEL_MASTER`        | `ragent-broker`  | Master name for broker instance (mode=sentinel). |
| `REDIS_RATELIMIT_SENTINEL_MASTER`     | `ragent-ratelimit` | Master name for rate-limiter instance (mode=sentinel). |

#### 4.6.4 Third-party API endpoints & credentials

| Variable | Default | Description |
|---|---|---|
| `AI_API_AUTH_URL`                     | (required)       | TokenManager J1→J2 endpoint (`POST /auth/api/accesstoken`). |
| `AI_LLM_API_J1_TOKEN`                 | (required, local) | J1 token for LLM service. POSTed as `{"key": value}`. **Never logged, never echoed.** |
| `AI_EMBEDDING_API_J1_TOKEN`           | (required, local) | J1 token for Embedding service. **Never logged, never echoed.** |
| `AI_RERANK_API_J1_TOKEN`              | (required, local) | J1 token for Rerank service. **Never logged, never echoed.** |
| `AI_USE_K8S_SERVICE_ACCOUNT_TOKEN`    | `false`          | When `true`, reads J1 from `/var/run/secrets/kubernetes.io/serviceaccount/token`; single shared J2 across all three services. Overrides the three `J1_TOKEN` vars. |
| `EMBEDDING_API_URL`                   | (required)       | bge-m3 endpoint. |
| `LLM_API_URL`                         | (required)       | gptoss-120b endpoint. |
| `RERANK_API_URL`                      | (required P2)    | Rerank endpoint (P1 unit-tests only; wired in P2). |
| `EMBEDDING_AUTH_HEADER_NAME`          | `Authorization`  | HTTP header name used by `EmbeddingClient`. Set to e.g. `X-API-Key` when the service does not use the `Authorization` header. Value sent is the raw J2 token (no `Bearer` prefix). |
| `LLM_AUTH_HEADER_NAME`                | `Authorization`  | HTTP header name used by `LLMClient`. Same semantics as `EMBEDDING_AUTH_HEADER_NAME`. |
| `RERANK_AUTH_HEADER_NAME`             | `Authorization`  | HTTP header name used by `RerankClient`. Same semantics as `EMBEDDING_AUTH_HEADER_NAME`. |
| `HR_API_URL`                          | (future)         | OpenFGA-related role lookup (P2+). |
| `CHATAGENT_API_URL`                   | (optional)       | POST `/chatagent/v1` proxy endpoint. When unset that route is not registered. |
| `CHATAGENT_SESSIONLIST_API_URL`       | (optional)       | GET `/chatagent/v1/sessionList` proxy endpoint. When unset that route is not registered. |
| `CHATAGENT_SESSION_API_URL`           | (optional)       | GET `/chatagent/v1/session` proxy endpoint. When unset that route is not registered. |
| `CHATAGENT_AP_NAME`                   | `ragent`         | `apName` injected into all outbound chatagent requests. |
| `CHATAGENT_AUTH`                      | (optional)       | Raw value for the `Authorization` header on all outbound chatagent calls (e.g. `Basic dXNlcjpwYXNz`). **Never logged, never echoed.** |
| `TWP_DEFAULT_MODEL`                   | (optional)       | Fallback model for `POST /twp/v1/run` when the request body omits `model`. |
| `UNPROTECT_ENABLED`                   | `false`          | When `true`, worker calls the unprotect API before passing `file`/`upload` ingest bytes to the pipeline. `ingest_type=inline` rows are always skipped (content is caller-supplied UTF-8 text). On unprotect failure the worker logs a warning and continues with the original MinIO bytes. |
| `UNPROTECT_API_URL`                   | (required when enabled) | Full URL of the unprotect endpoint (multipart POST). |
| `UNPROTECT_APIKEY`                    | (required when enabled) | Raw JWT (no `Bearer` prefix) sent as `apikey` request header. **Never logged, never echoed.** |
| `UNPROTECT_DELEGATED_USER_SUFFIX`     | (required when enabled) | Appended to `X-User-Id` to form the `delegatedUser` form field: `{X-User-Id}{suffix}`. |
| `EMBEDDING_REGISTRY_TTL_SECONDS`      | `10`             | B50 — TTL on the `ActiveModelRegistry` cache of `system_settings.embedding.*`. A cutover/rollback takes effect on the next App-cache refresh within this many seconds; the `dual_write_warmup` preflight gate refuses cutover until `2 × TTL` has elapsed since promote. |
| `COMMIT_MIN_HOURS`                    | `24`             | B50 — minimum observation window in `CUTOVER` state before `/embedding/v1/commit` is allowed (soft gate; override with `force=true`). Discourages impulsive commits that would retire the old stable field before issues surface. |

#### 4.6.5 Worker, Reconciler & retry policy

| Variable | Default | Description |
|---|---|---|
| `WORKER_HEARTBEAT_INTERVAL_SECONDS`   | `30`             | How often the worker refreshes `documents.updated_at` during pipeline body (B16). |
| `WORKER_MAX_ATTEMPTS`                 | `5`              | Pipeline gives up and marks `FAILED` once `attempt > WORKER_MAX_ATTEMPTS` (§3.1 R5). |
| `PIPELINE_TIMEOUT_SECONDS`            | `1800`           | Overall pipeline-body wall-clock ceiling (B18). |
| `RECONCILER_PENDING_STALE_SECONDS`    | `300`            | Re-dispatch threshold for `PENDING` rows whose heartbeat aged past this. |
| `RECONCILER_UPLOADED_STALE_SECONDS`   | `300`            | Re-kiq threshold for `UPLOADED` orphans (R1: TaskIQ message lost / broker outage at POST). |
| `RECONCILER_DELETING_STALE_SECONDS`   | `300`            | Resume threshold for stuck `DELETING` cascades. |

#### 4.6.6 Pipeline & chat tunables

| Variable | Default | Description |
|---|---|---|
| `INGEST_INLINE_MAX_BYTES`             | `10485760`       | v2: 10 MB cap on inline `content` UTF-8 byte length; 413 on overrun. |
| `INGEST_FILE_MAX_BYTES`                | `52428800`      | v2: 50 MB cap on file-type ingest size (HEAD-probe at API time); 413 on overrun. |
| `INGEST_LIST_MAX_LIMIT`               | `100`            | `GET /ingest/v1?limit=` upper bound (§4.1, B7). |
| `INGEST_MAX_ARCHIVE_MEMBERS`          | `5000`           | DOCX/PPTX zip-archive preflight: max entries in `infolist()`; 413 `INGEST_ARCHIVE_UNSAFE` on overrun (T-SEC.3/.4). |
| `INGEST_MAX_ARCHIVE_RATIO`            | `100`            | DOCX/PPTX zip-archive preflight: max `sum(file_size) / len(raw)` ratio; 413 `INGEST_ARCHIVE_UNSAFE` on overrun. |
| `INGEST_MAX_ARCHIVE_EXPANDED_BYTES`   | `524288000`      | DOCX/PPTX zip-archive preflight: 500 MB cap on `sum(file_size)` and per-member `file_size`; 413 `INGEST_ARCHIVE_UNSAFE` on overrun. |
| `INGEST_MAX_PDF_PAGES`                | `2000`           | PDF preflight: cap on `fitz.Document.page_count` before per-page extraction; 413 `INGEST_PDF_TOO_MANY_PAGES` on overrun (T-SEC.5/.6). |
| `INGEST_PDF_MARGIN_PTS`               | `0`              | PDF header/footer exclusion zone in PDF points (1 pt ≈ 0.35 mm); clipped from top and bottom of each page by `pymupdf4llm.to_markdown`; `0` disables. |
| `CHUNK_TARGET_CHARS`                  | `1000`           | v2 `_BudgetChunker` target chars (mime-agnostic). |
| `CHUNK_MAX_CHARS`                     | `1500`           | v2 `_BudgetChunker` hard cap; atoms above this are hard-split. |
| `CHUNK_OVERLAP_CHARS`                 | `100`            | v2 `_BudgetChunker` overlap between adjacent chunks. |
| `EMBEDDER_BATCH_SIZE`                 | `32`             | Chunks per embedder HTTP call (P-B). |
| `CHAT_JOIN_MODE`                      | `rrf`            | `rrf` \| `concatenate` \| `vector_only` \| `bm25_only` (C6). |
| `CHAT_RERANK_ENABLED`                 | `true`           | Insert `_Reranker` between joiner and `_SourceHydrator` (F1). |
| `RETRIEVAL_TOP_K`                     | `20`             | Cap applied to retrievers, joiner, and reranker (F7). |
| `RETRIEVAL_MIN_SCORE`                 | *(unset)*        | Global default score floor for `/retrieve/v1` and `/chat/v1`; unset = no filtering (`null`). Must be >= 0.0 if set; boot fails otherwise. |
| `EXCERPT_MAX_CHARS`                   | `512`            | `_ExcerptTruncator` truncation length (B23). |
| `RAGENT_DEFAULT_LLM_PROVIDER`         | `openai`         | Echoed when request omits `provider`. |
| `RAGENT_DEFAULT_LLM_MODEL`            | `gptoss-120b`    | Echoed when request omits `model`. |
| `RAGENT_DEFAULT_LLM_TEMPERATURE`      | `0.7`            | |
| `RAGENT_DEFAULT_LLM_MAX_TOKENS`       | `4096`           | |
| `RAGENT_DEFAULT_RAG_SYSTEM_PROMPT`    | *(multi-intent template)*     | System prompt used when the caller has no `system` message (regardless of retrieval result). Contains grounding rules + GREETING/QUESTION/SUMMARY/GENERATION intent styles with few-shot examples. No `{context}` placeholder — context is injected into the user message (empty retrieval injects `"(The context is empty.)"` sentinel). |
| `RAGENT_RAG_GROUNDING_RULES`          | *(rules-only variant)*        | Rules-only system prompt prepended when the caller supplies their own `system` message (regardless of retrieval result). Preserves the caller's persona while enforcing context-only grounding. Empty retrieval injects `"(The context is empty.)"` sentinel into the user message. |
| `CHAT_RATE_LIMIT_PER_MINUTE`          | `30`             | Per-user request cap on `/chat/v1` + `/chat/v1/stream` within the rate-limit window (B31). Excess returns 429 `CHAT_RATE_LIMITED`. |
| `CHAT_RATE_LIMIT_WINDOW_SECONDS`      | `60`             | Fixed-window length for `CHAT_RATE_LIMIT_PER_MINUTE` (B31). |
| `MCP_REQUEST_MAX_BYTES`               | `262144` (256 KiB) | Defence-in-depth cap on `POST /mcp/v1` request bodies; over-limit returns HTTP 413 `application/problem+json` (§3.8.1). |
| `CHAT_FEEDBACK_ENABLED`               | `false`          | Master switch for the feedback retrieval signal (B54). `true` enables `POST /feedback/v1`, the `_FeedbackMemoryRetriever` 3rd RRF input, and requires `FEEDBACK_HMAC_SECRET`. Default off — ship dark, observe write volume first (B57). |
| `CHAT_FEEDBACK_RRF_WEIGHT`            | `0.5`            | Weight on the feedback retriever's contribution in `DocumentJoiner(weights=[1.0, 1.0, this])` (B54). Cap < 1.0 to prevent popularity-loop dominance. |
| `CHAT_FEEDBACK_MIN_VOTES`             | `3`              | `(likes + dislikes)` threshold below which a (source_app, source_id) is dropped from the retriever (B54). Defeats single-user signal poisoning. |
| `CHAT_FEEDBACK_HALF_LIFE_DAYS`        | `14`             | Score decay half-life applied to the per-source Wilson score: `score × 0.5 ** (age_days / this)` (B54). |
| `FEEDBACK_HMAC_SECRET`                | *(required when `CHAT_FEEDBACK_ENABLED=true`)* | HMAC key for signing `/chat` response tokens and verifying `POST /feedback/v1` payloads (B55). Boot fails when feedback is enabled but the secret is unset. |

> **MCP protocol pins are NOT env-driven** — `protocolVersion` (`2024-11-05`) and `serverInfo.name` (`ragent`) are **pinned in spec §3.8.1 / B47** and live as module-level constants in `src/ragent/routers/mcp.py`. Operators flipping the protocol version would silently break the contract; the pin is intentional. The only MCP env knob is the body cap above.

#### 4.6.7 Per-call timeouts (matches §4.3 catalog)

| Variable | Default (s) | Site |
|---|---|---|
| `EMBEDDER_INGEST_TIMEOUT_SECONDS`     | `30`             | per-batch (32 strings) ingest call. |
| `EMBEDDER_QUERY_TIMEOUT_SECONDS`      | `10`             | single-string chat-query call (C8 asymmetric). |
| `ES_BULK_TIMEOUT_SECONDS`             | `60`             | `VectorExtractor` bulk index/delete. |
| `ES_QUERY_TIMEOUT_SECONDS`            | `10`             | chat retrievers (vector + BM25). |
| `MINIO_GET_TIMEOUT_SECONDS`           | `30`             | worker download from staging. |
| `MINIO_GET_RETRIES`                   | `3`              | max attempts (including first) for `MinioSiteRegistry.get_object()` transient errors; clamped to ≥ 1. |
| `MINIO_GET_RETRY_DELAY_SECONDS`       | `2.0`            | sleep between `get_object()` retry attempts (seconds). |
| `MINIO_PUT_TIMEOUT_SECONDS`           | `60`             | router upload to staging. |
| `LLM_TIMEOUT_SECONDS`                 | `120`            | `LLMClient.{chat\|stream}`. |
| `CHATAGENT_TIMEOUT_SECONDS`           | `30`             | per-call timeout for all chatagent proxy HTTP calls. |
| `PLUGIN_FAN_OUT_TIMEOUT_SECONDS`      | `60`             | per-plugin `extract`/`delete` ceiling (§3.3). |
| `READYZ_PROBE_TIMEOUT_SECONDS`        | `2`              | per-dependency `/readyz` probe budget (§4.1). |
| `UNPROTECT_TIMEOUT_SECONDS`           | `30`             | per-call budget for the unprotect API POST (when `UNPROTECT_ENABLED=true`). |

> Timeouts above are intentionally asymmetric: ingest embedder uses 30 s/batch (32 strings), query embedder uses 10 s (1 string) (C8). Same client, two call sites, two budgets.

#### 4.6.8 Observability (OpenTelemetry)

| Variable | Default | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | (optional)       | OTLP collector URL; absence disables export (no-op tracer). |
| `OTEL_SERVICE_NAME`                   | `ragent-api`     | Per-process: `ragent-api` \| `ragent-worker` \| `ragent-reconciler`. |
| `OTEL_TRACES_SAMPLER`                 | `parentbased_traceidratio` | Standard OTEL SDK sampler name. |
| `OTEL_TRACES_SAMPLER_ARG`             | `0.1`            | Sampling ratio (10% by default; raise to `1.0` in dev). |
| `HAYSTACK_TELEMETRY_ENABLED`          | `false`          | Disable Haystack anonymous usage analytics (PostHog). Set `false` for privacy/compliance. |
| `HAYSTACK_CONTENT_TRACING_ENABLED`    | `false`          | Include prompts and answers in OTEL spans. Keep `false` unless debugging; sensitive data. |
| `RAGENT_METRICS_SOURCE_APP_ALLOWLIST` | (empty)          | Comma-separated allow-list of `source_app` values that pass through verbatim as a Prometheus label. Anything outside the list is collapsed to `RAGENT_METRICS_SOURCE_APP_FALLBACK` to bound label cardinality. |
| `RAGENT_METRICS_SOURCE_APP_FALLBACK`  | `other`          | Bucket name for `source_app` values not in the allow-list. |
| `HTTP_ERROR_LOG_MAX_BYTES`            | `8192`           | Max bytes of request/response body included in `http.upstream_error` log records. Bodies above this size are truncated with `request_truncated` / `response_truncated` set to `true`. Sensitive headers (`Authorization`, `apikey`, `Cookie`, `X-API-Key`, `Proxy-Authorization`, plus the configured values of `EMBEDDING_AUTH_HEADER_NAME` / `LLM_AUTH_HEADER_NAME` / `RERANK_AUTH_HEADER_NAME`) and the J1 `key` field of the auth POST are always redacted regardless of size. |
