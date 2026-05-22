# 00_spec.md — Distributed RAG Agent

> Source: `docs/draft.md` · Standard: `docs/00_rule.md`

---

## 1. Mission

- Enterprise internal knowledge retrieval backend.
- Streaming chat answers grounded in private documents.
- Pluggable extractor architecture: graph reasoning (P3) without pipeline rewrite.

### Auth Modes (switchable; enforced by startup guard)
- **Mode A — open auth** (`RAGENT_AUTH_DISABLED=true`): no auth surface; `X-User-Id` header trusted; recorded as `documents.create_user` (audit only, not authorization). Guard requires `RAGENT_ENV=dev` AND `RAGENT_HOST=127.0.0.1` — loopback dev only.
- **Mode B — trust-header** (`RAGENT_AUTH_DISABLED=false`, `RAGENT_TRUST_X_USER_ID_HEADER=true`): JWT middleware bypassed; `X-User-Id` header trusted directly. Guard requires `RAGENT_ENV=dev` — dev override only.
- **Mode C — OIDC JWT** (both flags false): full JWKS-backed JWT verification (§3.5). Any env, any bind. Guard requires `OIDC_DOMAIN` and `OIDC_AUDIENCE`.
- Permission gating remains **DISABLED**. The Permission Layer (§3.5) ships in **P2**, backed by OpenFGA, and stays out of the retrieval/ES path.

---

## 2. Phase 1 Scope

| In P1 | Deferred |
|---|---|
| Ingest CRUD (Create / Read / List / Delete) with cascade | Permission Layer (OpenFGA) → P2 |
| Indexing Pipeline (§3.2) + Chat Pipeline (§3.4) | AsyncPipeline → P2 |
| Plugin Protocol v1, VectorExtractor, StubGraphExtractor | GraphExtractor → P3 |
| Third-party clients: Embedding, LLM, Rerank, TokenManager | Rerank wiring → P2 |
| Reconciler + locking | MCP real handler → P2 |
| Observability: OTEL auto-trace | — |

---

## 3. Domains

### 3.1 Ingest Lifecycle

> **v2 OVERRIDE (2026-05-06):** `POST /ingest` is **JSON only**. `inline`: `{ingest_type, mime_type, content, source_id, source_app, source_title, source_meta?, source_url?}` (UTF-8, ≤`INGEST_INLINE_MAX_BYTES`/10 MB, staged to `__default__`). `file`: replaces `content` with `minio_site`+`object_key`; HEAD-probed; ≤`INGEST_FILE_MAX_BYTES`/50 MB; no copy. MIME allow-list: `text/plain`, `text/markdown`, `text/html`, DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`), PPTX (`application/vnd.openxmlformats-officedocument.presentationml.presentation`), PDF (`application/pdf`) — else 415 `INGEST_MIME_UNSUPPORTED`; CSV dropped. `source_meta?` ≤1024 (B35); `source_url?` ≤2048. `MINIO_SITES` JSON → `MinioSiteRegistry`; `__default__` mandatory; `read_only=true` sites refuse post-READY delete.
>
> **Cleanup by `ingest_type`:** `inline` — post-READY auto-delete; `file` — never (caller-owned); `upload` (`POST /ingest/v1/upload` multipart, staged to `__default__`) — explicit DELETE only. Chunks live **only** in ES `chunks_v1` (MariaDB `chunks` dropped). Per-step logging: `event=ingest.step.{started,ok,failed}` + `event=ingest.ready` (§3.7). State machine / locking / heartbeat / supersede / reconciler unchanged.

**State machine:** `UPLOADED → PENDING → READY | FAILED`; `DELETING` transient on delete.

**Locking:** `UPDATE … WHERE status IN (:accept_set)`; `rowcount=1` = won, `rowcount=0` = lost. TX-A claims, TX-B commits terminal; pipeline body outside DB tx. Reconciler uses `SKIP LOCKED`. Invalid transition → `IllegalStateTransition`. **Heartbeat (B16):** worker updates `updated_at = NOW()` every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (30 s); Reconciler threshold = 5 min. **Timeout (B18):** `PIPELINE_TIMEOUT_SECONDS` (1800 s); overrun → `FAILED(PIPELINE_TIMEOUT)` + cleanup. **Object key (B10):** `{source_app}_{source_id}_{document_id}` sanitized to `[A-Za-z0-9._-]`; bucket = `MINIO_BUCKET` env.

**Retry idempotency:** each run begins with `delete_by_document_id` + `VectorExtractor.delete` (idempotent ES delete); no duplicate chunks on retry. **Supersede:** `(source_id, source_app)` is logical identity; POST always creates fresh `document_id`; on READY → keeps `MAX(created_at)` survivor, cascade-deletes rest. No PUT/PATCH.

**Create flow:** (1) `POST /ingest` → validation → MinIO upload → `documents(UPLOADED)` → kiq → 202. (2) Worker: TX-A claim; heartbeat; pipeline body: `delete_by_document_id` → §3.2 → `fan_out` → TX-B commit; post-commit MinIO delete + kiq supersede. (3) Supersede: loop `SELECT oldest READY FOR UPDATE SKIP LOCKED → cascade-delete → commit` until 1 row left.

**Delete flow:** atomic claim `DELETING`; outside-tx: `fan_out_delete` → `delete_by_document_id` → MinIO delete → row delete → 204. `rowcount=0` → silent 204. Mid-cascade failure → row stays `DELETING`; Reconciler resumes.

**BDD:**
- **S1** POST 1 MB `.txt` → 202 + 26-char task_id; status → `READY` within 60 s; chunks in ES.
- **S10** Illegal transitions (e.g. `READY→PENDING`) raise `IllegalStateTransition`.
- **S12** DELETE cascade on `READY` doc → `DELETING` → all plugins called once → ES/row cleared → 204 (MinIO already cleared at READY).
- **S13** Any failure mid-delete → row stays `DELETING`; Reconciler resumes ≤ 5 min.
- **S16** Pipeline reaches terminal state (`READY` or `FAILED`) → MinIO object deleted; subsequent re-processing not possible without re-upload.
- **S14** Re-DELETE an already-deleted document → 204, no plugin calls.
- **S15** `GET /ingest?limit=2` on 5 docs → ≤ 2 items + `next_cursor` continues.
- **S17** New POST of same `(source_id,source_app)` → D2 created; D1 queryable while D2 `PENDING`; after D2 `READY` supersede cascade-deletes D1.
- **S18** D2 with same pair → `FAILED` → D1 stays `READY`; supersede not enqueued.
- **S19** Supersede re-delivery → no-ops (≤1 `READY` row).
- **S20** D1(t=0) and D2(t=1) both reach `READY` out-of-order → only D2 (`MAX(created_at)`) survives.
- **S24** `UPLOADED` row > 5 min → Reconciler re-kiqs `ingest.pipeline` → proceeds normally (R1).
- **S25** Reconciler retry → `delete_by_document_id` ensures no duplicate chunks; `chunks_v1` clean (R4).
- **S27** `FAILED` commit → `chunks` + ES `chunks_v1` cleared for that `document_id` (R5).
- **S28 worker claim race (R7)** — Two concurrent workers on the same `document_id`: InnoDB serialises on the row X-lock; winner proceeds (OVERWRITE idempotency keeps chunks consistent), loser gets `rowcount=0` → `event=ingest.claim_skipped`. No `LockNotAvailable`.
- **S31 supersede single-loser-per-tx (P-C)** — Given supersede must delete K=10 losers, When the task runs, Then each is deleted in its own committed tx (loop), not one tx holding K row-locks.
- **S41 manual rerun** — Status in `{UPLOADED, PENDING, FAILED}` → `POST /ingest/v1/{id}/rerun` flips to `PENDING`, resets `attempt=0`, clears `error_code`, re-kiqs, returns `202`. Status `READY|DELETING` → `409 INGEST_NOT_RERUNNABLE`. Unknown id → `404 INGEST_NOT_FOUND`.

---

### 3.2 Indexing Pipeline

> **v2 Pipeline:**
> ```
> _TextLoader → FileTypeRouter
>    ├ text/plain    → DocumentSplitter (Haystack stock, by passage)
>    ├ text/markdown → _MarkdownASTSplitter (mistletoe AST; atomic units = heading/code/list/table/blockquote; never splits inside fenced code)
>    ├ text/html     → _HtmlASTSplitter (selectolax; drops script/style/nav/aside/footer/header; atoms = heading/pre/table/article-paragraphs)
>    ├ docx          → _DocxASTSplitter (python-docx; paragraphs + tables)
>    ├ pptx          → _PptxASTSplitter (python-pptx; one atom per slide)
>    └ unclassified  → _RaiseUnroutable (worker → FAILED + PIPELINE_UNROUTABLE)
> → DocumentJoiner → _IdempotencyClean (ES delete by document_id)
> → _BudgetChunker (1000 target / 1500 max / 100 overlap, mime-agnostic)
> → DocumentEmbedder (bge-m3 batched) → DocumentWriter (ES chunks_v1 only)
> ```
> Each splitter sets `meta["raw_content"]` = exact byte slice (byte-stable, R4/S25). `_BudgetChunker` is the sole budget enforcer. `chunks_v1` stores both `content` (normalized, BM25-analyzed) and `raw_content` (`index: false`); LLM context and citations use `raw_content`.

**Timeouts & idempotency:** First step = `delete_by_document_id` + `fan_out_delete` (retry idempotency). Embedder batched 32 chunks/call. Timeouts: embedder 30 s/batch, ES bulk 60 s, MinIO 30 s, plugin 60 s, overall `PIPELINE_TIMEOUT_SECONDS` (1800 s). No DB tx during pipeline body (§3.1).

---

### 3.3 Pluggable Extractors

**Protocol v1 (frozen):**

```python
@runtime_checkable
class ExtractorPlugin(Protocol):
    name: str; required: bool; queue: str
    def extract(self, document_id: str) -> None: ...
    def delete(self, document_id: str) -> None: ...
    def health(self) -> bool: ...
```

**P1 plugins:** `VectorExtractor` (required, ES bulk), `StubGraphExtractor` (optional, no-op). See §4.4.

**Plugin construction (B17):** Protocol freezes interface; plugins are DI-constructed. `VectorExtractor.__init__(repo, chunks, embedder, es)` — `extract()` reads `source_title` from `repo`. Plugins MUST NOT import `pipelines/` or HTTP layers.

**Registry:**
- `register()` raises `DuplicatePluginError` on name conflict.
- `fan_out(document_id)` → dispatch extract to all plugins concurrently; **per-plugin timeout 60 s** (overrun → `Result(error="timeout")`); `all_required_ok(results)` gates `READY`.
- `fan_out_delete(document_id)` → dispatch delete to all plugins concurrently; **per-plugin timeout 60 s**; runs **outside any DB transaction** (no row locks held during plugin network calls — P-E).

**BDD:** S4 Protocol conformance — missing `name/required/queue/extract/delete/health` → `isinstance` false, `register()` raises. S11 duplicate registration → `DuplicatePluginError`.

---

### 3.4 Chat Pipeline

```
QueryEmbedder → {ESVectorRetriever (kNN on `embedding`, optional filter)
                 ∥ ESBM25Retriever (multi_match on `["text", "title^2"]`, optional filter)
                 ∥ FeedbackMemoryRetriever (kNN on `feedback_v1.query_embedding`, optional;
                                            present iff CHAT_FEEDBACK_ENABLED + CHAT_JOIN_MODE=rrf, B54)}
              → DocumentJoiner(RRF, weights=[1, 1, CHAT_FEEDBACK_RRF_WEIGHT])
              → SourceHydrator(JOIN documents)
              → LLMClient.{chat | stream}
```

**Title (B15):** embedded as `embed(f"{title}\n\n{text}")` (semantic) and BM25 `multi_match ["text","title^2"]` (lexical). No separate retriever.
**SourceHydrator gate (B36):** chunks with no `READY` `documents` row are **dropped** — orphans/in-flight/DELETING rows never reach LLM or `sources[]`.
**Filter (B29→B35):** optional `source_app`/`source_meta` → ES `term` filter on both retrievers; AND when both supplied.
**Two endpoints (B12):** `POST /chat` (sync JSON) and `POST /chat/stream` (SSE: `delta` + terminal `done`). Same schema.
**Join mode (`CHAT_JOIN_MODE`):** `rrf` (default, RRF k=60) | `concatenate` | `vector_only` | `bm25_only`. Factory assembles graph at startup. P1: no permission gating, retrievers run sequentially; P2 adds `AsyncPipeline` concurrency and `PermissionClient` post-filter (§3.5).

#### 3.4.1 Request schema (shared by `/chat` and `/chat/stream`)

```json
{
  "messages":         [{"role": "system|user|assistant", "content": "..."}],
  "provider":         "openai",
  "model":            "gptoss-120b",
  "temperature":      0.7,
  "maxTokens":        4096,
  "source_app":       "confluence",
  "source_meta":      "engineering",
  "top_k":            20,
  "min_score":        null
}
```

- `messages` required; all other optional (defaults as shown). `source_app` (≤64) / `source_meta` (≤1024): ES `term` filters AND when both; empty → 422 `CHAT_FILTER_INVALID`. `top_k` range 1–200. `provider` validated against `{"openai"}` (B22), 422 `CHAT_PROVIDER_UNSUPPORTED` otherwise. Missing `system` → server prepends default. Retrieval query = last `user` message.

#### 3.4.2 Response schema

`/chat` (`application/json`) and terminal `done` of `/chat/stream`:
```json
{"content":"…","usage":{"promptTokens":0,"completionTokens":0,"totalTokens":0},"model":"gptoss-120b","provider":"openai",
 "sources":[{"document_id":"…","source_app":"…","source_id":"…","source_meta":"…","type":"knowledge",
             "source_title":"…","source_url":"…","mime_type":"…","excerpt":"…","score":0.87}],
 "request_id":"…","feedback_token":"<base64url>.<hmac_hex>"}
```

`sources` is `null` when empty; `type` always `"knowledge"` in P1. `excerpt` truncated to `EXCERPT_MAX_CHARS` (512) — LLM receives full text. `usage` non-streaming only. `request_id`+`feedback_token` present only when `CHAT_FEEDBACK_ENABLED=true` AND `X-User-Id` present (B55).

#### 3.4.3 Streaming wire format (`/chat/stream` only)

```
data: {"type":"delta","content":"<token chunk>"}\n\n
…
data: {"type":"done","content":"<full>","model":"…","provider":"…","sources":[…]}\n\n
```

**Error mid-stream (B6):** If the LLM or any retriever fails *after* the first `delta` has been written, the server emits a single default-event `data:` line with payload `{"type":"error","error_code":"<CODE>","message":"<text>"}` and closes the connection. **No `event: error` named-event is used.** Pre-stream failures (before the first `delta`) return a normal RFC 9457 problem+json response. `/chat` always uses problem+json on error (it has no streaming surface).

**BDD:**
- **S6**  — `POST /chat/stream` emits ≥ 1 `data: {type:"delta",...}` then exactly one `data: {type:"done",...}` carrying `content`, `usage`, `model`, `provider`, `sources`.
- **S6a** — `POST /chat` returns `200 application/json` with the full §3.4.2 body (single response, no streaming framing).
- **S6d** — Empty retrieval (index empty or retriever error) → response `sources: null` and the LLM still answers.
- **S6e** — Every emitted `sources[]` entry has all six fields populated and `type="knowledge"`.
- **S6j orphan chunk dropped (B36)** — ES chunk with no `READY` `documents` row is dropped by `_SourceHydrator`; never reaches `sources[]` or LLM context (applies to `/chat`, `/chat/stream`, `/retrieve`).

---

#### 3.4.4 `POST /retrieve` — Retrieval without LLM

Full retrieval pipeline (embed → kNN + BM25 → RRF → hydration), no LLM call. Returns ranked chunks for retrieval quality inspection and custom UIs.

**Request:** `{query, source_app?, source_meta?, top_k?, min_score?, dedupe?}`. Only `query` required. Filters same as `/chat` (B29→B35). `top_k` default 20, range 1–200. `min_score` default `null` (post-pipeline floor). `dedupe` default `false`.

**Response:** `{"chunks":[{document_id, source_app, source_id, source_meta, type:"knowledge", source_title, source_url, mime_type, excerpt, score}]}`. `chunks` is `[]` (never `null`) when empty. `excerpt` truncated to `EXCERPT_MAX_CHARS` (512) (B23). `dedupe=false`: same `document_id` may appear multiple times; `dedupe=true`: one entry per `document_id`, highest RRF score kept.

**BDD:**
- **S38 retrieve returns all chunks by default** — Given two chunks from the same `document_id` both rank in the top-K, When `POST /retrieve {"query":"..."}` (no `dedupe`), Then both appear in `chunks[]` with the same `document_id`.
- **S39 retrieve dedupe=true keeps best chunk** — Given the same two chunks, When `POST /retrieve {"query":"...","dedupe":true}`, Then exactly one entry with that `document_id` appears, and its `excerpt` matches the higher-scored chunk.
- **S40 retrieve empty index** — Given an empty ES index, When `POST /retrieve`, Then `{"chunks":[]}` is returned (not `null`).
- **S41 retrieve filter (B29 → B35)** — Same `source_app` / `source_meta` filter semantics as `/chat`; non-matching filter returns `{"chunks":[]}`.

---

#### 3.4.5 `POST /feedback/v1` — User feedback on chat sources (B54/B55/B56, T-FB.6)

Closes the feedback loop: client echoes back the HMAC-signed token from a prior `/chat` response and reports a vote against one of the shown sources. Dual-writes MariaDB `feedback` (§5.1) and ES `feedback_v1` (§5.4); next `/chat` consults the ES index via `_FeedbackMemoryRetriever` (B54) when `CHAT_FEEDBACK_ENABLED=true`.

**Request:** `{request_id, feedback_token, query_text, shown_sources: [{source_app, source_id}], source_app, source_id, vote, reason?, position_shown?}`. `request_id`/`feedback_token` from `/chat` response (§3.4.2), TTL 7 days; HMAC binds `request_id`, `user_id`, `sources_hash`; mismatch → 401 `FEEDBACK_TOKEN_INVALID`. Voted `(source_app, source_id)` must be ∈ `shown_sources`. `vote ∈ {+1,-1}`. `reason ∈ {irrelevant, hallucinated, outdated, incomplete, wrong_citation, other}` (B56, frozen) or omit. `position_shown` 0-based, recorded for future IPS.

**Response:** `204`; errors: `401 FEEDBACK_TOKEN_INVALID` (HMAC/token/request_id/user mismatch), `410 FEEDBACK_TOKEN_EXPIRED` (>7 days), `422 FEEDBACK_SOURCE_INVALID` (voted pair ∉ shown_sources), `422 FEEDBACK_VALIDATION` (schema violation).

**Dual-write (B55):** MariaDB `feedback` UPSERT keyed `(user_id, request_id, source_app, source_id)`; ES `feedback_v1` `_id = sha256(...)` — same key. Re-embeds `query_text` per call. ES failure → 204 + `ragent_feedback_es_write_failed_total` (MariaDB is truth).

**BDD:**
- **S42** valid token + shape → 204; MariaDB row + ES doc with `source_app`.
- **S43** byte flip → 401 `FEEDBACK_TOKEN_INVALID`.
- **S44** `ts` > 7 days → 410 `FEEDBACK_TOKEN_EXPIRED`.
- **S45** different `shown_sources` at sign time → 401 `FEEDBACK_TOKEN_INVALID`.
- **S46** voted pair ∉ `shown_sources` → 422 `FEEDBACK_SOURCE_INVALID`.
- **S47** reason outside B56 enum → 422.
- **S48** re-vote same key → 204, single row, `updated_at` advanced.
- **S49** ES write raises → 204 + `ragent_feedback_es_write_failed_total += 1`.
- **S52** `/chat` with `source_app=X` + `CHAT_FEEDBACK_ENABLED=true` → kNN filter `term:{source_app:X}` (no cross-app bleed).

---

### 3.5 Authentication & Permission

Two distinct concerns, kept architecturally separate from retrieval:

| Concern | Question answered | Mechanism | P1 | Future phase |
|---|---|---|---|---|
| **Authentication** | Who is the caller? | JWT verified by **joserfc** against OIDC `OIDC_DOMAIN` JWKS (signature + `iss` + `aud` + `exp`) → `user_id = <RAGENT_JWT_CLAIM_USER_ID>` claim | OFF — `<RAGENT_USER_ID_HEADER>` trusted, validated non-empty | FastAPI middleware verifies on every protected endpoint; `RAGENT_TRUST_X_USER_ID_HEADER=true` falls back to header (dev/integration override) |
| **Permission** | Can this caller see this document? | Permission Layer service that calls **OpenFGA** | OPEN — no checks, all docs visible | `PermissionClient.batch_check(user_id, document_ids)` returns the allowed subset; gated per-surface by `RAGENT_PERMISSION_INGEST_ENABLED` / `RAGENT_PERMISSION_CHAT_ENABLED` (both default `false` even in P2) |

**Design principle:** ES (`chunks_v1`) carries **no auth fields** in any phase — retrieval is permission-blind. The Permission Layer post-filters by `document_id`, keeping ES schema stable across phases.

**P1 (current phase):** No JWT — `<RAGENT_USER_ID_HEADER>` trusted, written to `documents.create_user` (audit only, not authz). No permission gating — all chunks visible. `auth_mode=open` in audit logs. **TokenManager (J1→J2) is active** for Embedding/LLM/Rerank API auth (unrelated to user auth).
**P2 additions:**
- **JWT:** raw token in `<RAGENT_JWT_HEADER>` (no `Bearer` prefix). **joserfc** verifies signature against JWKS at `{https}://<OIDC_DOMAIN>/.well-known/jwks.json`, `iss`, `aud == OIDC_AUDIENCE`, `exp`, `nbf`. JWKS fetched at `build_container()` and cached in-process (T8.1a). Claim `RAGENT_JWT_CLAIM_USER_ID` (default `preferred_username`) → `user_id`. Errors: 401 `AUTH_TOKEN_INVALID` / `AUTH_TOKEN_EXPIRED` / `AUTH_CLAIM_MISSING`. `RAGENT_TRUST_X_USER_ID_HEADER=true` bypasses JWT (dev only).
- **Public paths (no auth):** `/livez`, `/readyz`, `/startupz`, `/metrics`, `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json`. MCP Hub runs as a separate process, not covered by this middleware.
- **Header injection:** extracted `user_id` written into `<RAGENT_USER_ID_HEADER>` on request scope; routers read via `Depends(ragent.auth.deps.get_user_id)` (never `Header(alias=...)` directly — T8.D2/T8.D3).
- **PermissionClient (OpenFGA):** `batch_check(user_id, document_ids) → set[str]` post-filters retrieved chunks. Gated by `RAGENT_PERMISSION_INGEST_ENABLED` / `RAGENT_PERMISSION_CHAT_ENABLED` (both default `false`). ES carries no auth fields; permission is always post-retrieval only.

**BDD:**
- **S9 token refresh at boundary** — Given `TokenManager` cache holds a J2 with `expiresAt = T0 + 60 min`, When the wall clock advances to `T0 + 55 min` (`expiresAt − 5 min`) and a caller asks for the J2 token, Then `TokenManager` issues exactly one J1→J2 refresh HTTP exchange and returns the new token; 100 concurrent callers around the boundary share that single refresh (single-flight, P-F).
- Permission-gating BDD specified when the P2 plan is written.

---

### 3.6 Resilience

**Reconciler (K8s CronJob `*/5 * * * *`, `concurrencyPolicy: Forbid`) — B9:** `python -m ragent.reconciler` one-shot; not a TaskIQ task (broker-outage-independent).

- `UPLOADED, updated_at < NOW() - 5 min` → re-kiq `ingest.pipeline` (R1).
- `PENDING, updated_at < NOW() - 5 min, attempt ≤ 5` → stale heartbeat → re-kiq (idempotent key: `document_id + attempt`).
- `PENDING, updated_at < NOW() - 5 min, attempt > 5` → `FAILED` (cleanup) + `event=ingest.failed`.
- `DELETING > 5 min` → resume cascade delete idempotently.
- **Multi-READY invariant repair (R3):** every cycle also runs `SELECT source_id, source_app FROM documents WHERE status='READY' GROUP BY source_id, source_app HAVING COUNT(*) > 1` and re-enqueues `ingest.supersede` for each pair.
- **Heartbeat (R8):** every tick increments `reconciler_tick_total` and emits `event=reconciler.tick`. Prometheus alert fires if no tick observed for > 10 min (Reconciler is itself a single point of failure).

**BDD:** S2: `PENDING` >5 min + attempt≤5 → re-kiq (once/cycle). S3: attempt>5 → `FAILED`, cleanup, `event=ingest.failed`. See S24/S25.

**Infrastructure (B27):** Redis broker (TaskIQ) and rate-limiter are separate logical instances, both configured via `REDIS_MODE` (`standalone` | `sentinel`). Standalone: `REDIS_BROKER_URL` / `REDIS_RATELIMIT_URL`. Sentinel: `REDIS_SENTINEL_HOSTS` quorum + `REDIS_*_SENTINEL_MASTER` names. Both API and worker use the same code path.

#### 3.6.1 Chaos drill suite (P2.6 軌三 / T7.4.x)

Six cases C1–C6 (worker SIGKILL, MariaDB↔ES split-brain, ES bulk 207 partial, rerank 5xx, LLM stream interrupt, MinIO transient 503); each asserts terminal status, ES/DB consistency, OTEL spans, and `chaos_drill_outcome_total` counter. Gated by nightly CI; 3 consecutive green runs before P2.6 marked done.

---

### 3.7 Observability

- Haystack auto-trace + FastAPI OTEL middleware → Tempo + Prometheus.
- Structured logs for state-machine transitions; `auth_mode=open` field in P1.
- **Heartbeat metrics (R8):** `reconciler_tick_total` (counter); Prometheus alert when missing > 10 min. Worker emits `worker_pipeline_duration_seconds` (histogram) and `event=ingest.{started,failed,ready}`.
- **Orphan/leak counters:** `minio_orphan_object_total` (post-commit cleanup failure), `multi_ready_repaired_total` (Reconciler R3 sweep).
- **ES events (B26):** `event=es.bbq_unsupported` (cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW); `event=schema.drift` (resource file ↔ live mapping mismatch). Both surface in `/readyz` as degraded (B4).
- **Structured logging (structlog).** JSON to stdout. Categories: (1) **API trace** (`api.request/error`) — `{request_id, method, path, status_code, duration_ms, user_id, trace_id}` via `RequestLoggingMiddleware`. (2) **Business** — `chat.retrieval/llm`, `ingest.failed/ready`, `reconciler.tick`, etc., paired with OTEL spans. (2a) **Per-step pipeline** — `ingest.step.{started,ok,failed}` and `retrieve.step.{started,ok,failed}` carry `{step, duration_ms, atoms_in?, chunks_out?, error_code?}`; companion `{namespace}.step.ok.docs` event with `doc_refs: [{document_id, chunk_id, score}]` after each successful step. Dual-emitted as OTEL span `{namespace}.step.{step}` (T-APL.11). Cross-process correlation via `StructlogContextMiddleware` — `request_id`/`user_id` propagated into worker logs (T-APL.9). (3) **Error** — `error_type, error_code`, traceback. Format: ISO 8601 UTC. **Privacy:** denylist processor drops `query/prompt/messages/completion/chunks/embedding/documents/body/authorization/cookie/password/token/secret`; `HAYSTACK_CONTENT_TRACING_ENABLED` pinned off.

---

### 3.8 MCP Tool Server (P2.5)

Wraps `POST /retrieve/v1` (§3.4.4) as an MCP tool — no LLM call. Real MCP JSON-RPC 2.0 server (B47 Option B); P1 501 stub removed. Options A (REST-only) and C (dual surface) rejected.

#### 3.8.1 Protocol

- **Transport:** Streamable HTTP, request/response subset (POST only; no server-initiated SSE in P2.5). Pinned MCP spec revision: `"2024-11-05"`.
- **Endpoint:** `POST /mcp/v1` (single endpoint; method dispatched from JSON-RPC `method` field).
- **Envelope:** JSON-RPC 2.0 — `{"jsonrpc":"2.0","id":...,"method":"...","params":{...}}` / `{"result":{...}}` / `{"error":{"code":<int>,"message":"...","data":{...}?}}`.
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
| `tools/list` | client → server | Returns `{tools: [{name, description, inputSchema}]}`. |
| `tools/call` | client → server | Invokes a tool. Returns `{content: [{type, text}], isError}`. |
| `ping` | bidirectional | Returns `{}`. Optional keepalive. |

Any other method → JSON-RPC error `-32601 Method not found`.

#### 3.8.3 The `retrieve` tool

The sole tool advertised by `tools/list`. Mirrors §3.4.4 `POST /retrieve/v1` semantics. `inputSchema`: `required: ["query"]`; properties: `query` (string, minLength 1), `top_k` (integer 1–200, default 20), `source_app` (string ≤64), `source_meta` (string ≤1024), `min_score` (number ≥0), `dedupe` (boolean, default false).

**Result shape:** `{"content":[{"type":"text","text":"{\"chunks\":[...]}"}],"isError":false}`. `content[0].text` = JSON-stringified `RetrieveResponse` (§3.4.4 shape). `isError: true` when tool fails; transport failures use `error` envelopes.

#### 3.8.4 Error codes (JSON-RPC layer)

| Code | Meaning | Origin |
|---|---|---|
| `-32700` | Parse error (malformed JSON) | Transport |
| `-32600` | Invalid Request (missing `jsonrpc` / `method`, etc.) | Transport |
| `-32601` | Method not found | Dispatch |
| `-32602` | Invalid params (e.g. `tools/call` with unknown `name`, or `inputSchema` validation fail) | Dispatch |
| `-32603` | Internal error | Server |
| `-32001` | Tool execution failed (retrieval pipeline error; mirrors `MCP_TOOL_EXECUTION_FAILED`) | App |

App-level errors (-32000..-32099) carry `data.error_code` matching the existing `HttpErrorCode` catalog (§4.1.2).

#### 3.8.5 BDD

- **S58 mcp initialize** — `initialize` with `protocolVersion:"2024-11-05"` → `result.{protocolVersion:"2024-11-05", capabilities:{tools:{}}, serverInfo:{name:"ragent",version:"<semver>"}}`.
- **S60 mcp tools/call retrieve** — Given indexed corpus and `tools/call` with `{name:"retrieve", arguments:{query:"...",top_k:3}}`, When the server processes it, Then `result.content[0].text` is JSON parseable into `{chunks: list}` of length ≤ 3 and `result.isError` is `false`.
- **S61 mcp method not found** — Given `{method:"resources/list"}` (unimplemented), Then `error.code` is `-32601`.
- **S63 mcp tools/call missing query** — Given `{method:"tools/call", params:{name:"retrieve",arguments:{}}}` (no `query`), Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_INPUT_INVALID`.
- **S64 mcp parse error** — Given a request body that is not valid JSON, Then HTTP `200` with JSON-RPC body `{jsonrpc:"2.0",id:null,error:{code:-32700,...}}` (per JSON-RPC 2.0 §5: `id` is `null` when parse failed).
- **S65 mcp notifications/initialized** — Given `{jsonrpc:"2.0", method:"notifications/initialized"}` (no `id`), Then HTTP `204` with empty body; no JSON-RPC response object emitted.

### 3.9 MCP Hub Microservice

Standalone FastMCP-based service (`src/ragent/mcp_hub/`) that loads `tools.yaml` files at startup and dynamically registers each declared REST endpoint as an MCP tool. Different scope from §3.8: §3.9 federates arbitrary third-party REST APIs into one MCP surface; it holds no upstream tokens — identity flows via per-request MCP-client headers.

#### 3.9.1 Process and transport

- **Entry point:** `python -m ragent.mcp_hub.server`. Transport: FastMCP Streamable HTTP at `MCP_HUB_PATH`.
- **Registry source:** `MCP_HUB_TOOLS_YAML` (default `tools.yaml`); single file or directory. Directory mode: each `*.yaml`/`*.yml` is one SYSTEM (name = filename stem); tool names qualify as `<system>.<tool>`.
- **Per-system isolation:** each system gets its own `httpx.AsyncClient`. Shutdown closes every client via `server.build_app(bundle)` lifespan.

#### 3.9.2 Env-var inventory

| Var | Default | Purpose |
|---|---|---|
| `MCP_HUB_TOOLS_YAML` | `tools.yaml` | File or directory; directory mode = multi-system |
| `MCP_HUB_NAME` | `ragent-mcp-hub` | Server name advertised in `initialize` |
| `MCP_HUB_HOST` | `0.0.0.0` | Bind host |
| `MCP_HUB_PORT` | `9000` | Bind port (non-numeric → `SystemExit`) |
| `MCP_HUB_PATH` | `/mcp` | Streamable HTTP mount path |

The Hub reads NO secrets from env — those flow via per-request MCP-client headers (§3.9.4).

#### 3.9.3 `tools.yaml` schema

Top-level: `system` (name), `defaults` (`base_url`, `timeout`, `max_connections`, `verify_ssl` — MUST be explicit yaml boolean, `headers` map), `tools[]` (`name`, `description`, `method`, `path`, `forward_headers`, `parameters[]` with `name`/`type`/`location ∈ {path,query,body,header}`/`required`).

Load-time validation (enforced by `doctor`): duplicate tool names, unmatched path placeholders, body params on non-body methods, static/forward header conflicts, missing `base_url` when any path is relative → all rejected. One bad yaml isolates: rest of registry still serves; failures surface on `HubBundle.failures`.

#### 3.9.4 Header forwarding

`HeaderForwardMiddleware` lowercases incoming headers into a request-scoped `ContextVar`. Template `{header-name}` placeholders in `forward_headers` reference incoming headers; missing placeholder → header skipped. Merge order: `defaults.headers` → `static_headers` → `forward_headers` → `location: header` args. Deploy behind mTLS — LLM never controls header values.

#### 3.9.5 Response envelope

```json
{"ok": true,  "status": 200, "data": <json-or-text-body>}
{"ok": false, "status": 404, "error": {"type": "upstream_4xx", "status": 404, "upstream_body": ..., "upstream_request_id": "..."}}
```

5xx / timeout / connect errors raise `ToolError`; 5xx bodies redacted (status + request_id only).

#### 3.9.6 Doctor

`python -m ragent.mcp_hub.doctor` — CI yaml validator. Exit 0/1/2 (clean/schema-error/missing-file). Make target: `make mcp-hub-doctor` (chained into `make check`).

#### 3.9.7 Structured logging

| Event | Level | Fields |
|---|---|---|
| `mcp_hub.system_configured` | INFO | `system`, `base_url`, `timeout`, `max_connections` |
| `mcp_hub.ready` | INFO | `systems`, `tool_count`, `failure_count` |
| `mcp_hub.load_failure` | WARN | `source`, `reason` |
| `mcp_hub.tool_call.success` | INFO | `tool`, `system`, `status`, `latency_ms`, `request_id` |
| `mcp_hub.upstream_4xx` | WARN | + `upstream_request_id` |
| `mcp_hub.upstream_5xx` / `mcp_hub.timeout` / `mcp_hub.connect_error` | ERROR | `tool`, `latency_ms` |

SECURITY: header VALUES (Authorization, JWT, API keys) are NEVER logged (test-pinned).

#### 3.9.8 Prometheus metrics (`GET /metrics`)

| Metric | Type | Labels |
|---|---|---|
| `mcp_hub_tool_load_failures_total` | Counter | `system`, `phase` ∈ {`file_parse`, `tool_parse`, `registration`} |
| `mcp_hub_tool_calls_total` | Counter | `system`, `tool`, `outcome` ∈ {`success`, `upstream_4xx`, `upstream_5xx`, `timeout`, `connect_error`} |
| `mcp_hub_tool_call_duration_seconds` | Histogram | `system`, `outcome` (`tool` omitted to bound cardinality) |

---

## 4. Inventories

### 4.1 Endpoints

> **v2 OVERRIDE for `POST /ingest`** — JSON body only (no multipart). `inline`: `{ingest_type, mime_type, content, source_id, source_app, source_title, source_meta?, source_url?}`. `file`: adds `minio_site` + `object_key`, omits `content`.
> Validation order: discriminator-shape (422) → `mime_type` allow-list (415) → size cap (413) → `minio_site` resolved (422 `INGEST_MINIO_SITE_UNKNOWN`) → file HEAD-probe (422 `INGEST_OBJECT_NOT_FOUND`). Worker-side: DOCX/PPTX zip preflight → 413 `INGEST_ARCHIVE_UNSAFE`; PDF page-count → 413 `INGEST_PDF_TOO_MANY_PAGES`. Every guard rejection increments `ragent_ingest_rejected_total{reason}` (T-SEC.7).

| Method | Path | P1 Auth | Request | Response |
|---|---|---|---|---|
| POST   | `/ingest/v1`               | `X-User-Id` | **JSON** (v2, see override above) | `202 { task_id }` — `task_id` **is** the `document_id`. |
| GET    | `/ingest/v1/{id}`          | `X-User-Id` | — | `200 { status, attempt, updated_at }` |
| GET    | `/ingest/v1?after=&limit=&source_id=&source_app=` | `X-User-Id` | — | `200 { items, next_cursor }` (limit ≤ 100; ordered `document_id DESC`; `source_id`/`source_app` are optional exact-match filters) |
| DELETE | `/ingest/v1/{id}`          | `X-User-Id` | — | `204` idempotent |
| POST   | `/ingest/v1/{id}/rerun`    | `X-User-Id` | — | `202 { document_id }` — manual re-dispatch of `ingest.pipeline` for non-READY/non-DELETING rows; `404 INGEST_NOT_FOUND` / `409 INGEST_NOT_RERUNNABLE` per S41. |
| POST   | `/ingest/v1/upload`        | `X-User-Id` | `multipart/form-data` (server stages to `__default__` MinIO; identical downstream to inline) | `202 { document_id }` |
| POST   | `/retrieve/v1`             | `X-User-Id` | §3.4.4 schema (`query` required; rest default) | `200 { chunks[] }` per §3.4.4 |
| POST   | `/chat/v1`                 | `X-User-Id` | §3.4.1 schema (`messages` required; rest default) | `200 application/json` per §3.4.2 |
| POST   | `/chat/v1/stream`          | `X-User-Id` | §3.4.1 schema | `text/event-stream` per §3.4.3 (`data: {type:delta\|done\|error}`) |
| POST   | `/feedback/v1`             | `X-User-Id` | §3.4.5 schema | `204` on success; `401`/`410`/`422` `application/problem+json` per §3.4.5. |
| POST   | `/mcp/v1`               | `<RAGENT_USER_ID_HEADER>` (P1) / `<RAGENT_JWT_HEADER>` (P2) | JSON-RPC 2.0 envelope per §3.8 | `200` with JSON-RPC response envelope; `204` for `notifications/*`. Auth failure (401) returns `application/problem+json` per §3.8.1 (transport-layer). |
| GET    | `/livez`                | none        | — | `200 {"status":"ok"}` — process up; no dependency probes |
| GET    | `/readyz`               | none        | — | `200` if all dep probes pass; else `503 application/problem+json` listing failed deps. Probes: **MariaDB** (`SELECT 1`), **ES** (`GET /_cluster/health` + `analysis-icu` plugin loaded + every `resources/es/*.json` index exists; B26, I5), **Redis broker & rate-limiter** (`PING` against active topology per `REDIS_MODE`; B27), **MinIO** (`ListBuckets`). Each probe ≤ 2 s. |
| GET    | `/metrics`              | none        | — | `200 text/plain; version=0.0.4` — Prometheus exposition (counters/histograms in §3.7) |

Future-phase auth: JWT verify (auth) + `PermissionClient` post-retrieval gate (permission, OpenFGA-backed) — see §3.5. ES queries remain permission-blind in every phase.

### 4.1.1 Error Response Schema (B5)

All non-2xx responses use **RFC 9457 Problem Details** (`Content-Type: application/problem+json`), extended with a business-semantic `error_code`:

```json
{
  "type":        "https://ragent.dev/errors/ingest-mime-unsupported",
  "title":       "Unsupported media type",
  "status":      415,
  "detail":      "MIME 'image/png' is not in the P1 allow-list",
  "instance":    "/ingest",
  "error_code":  "INGEST_MIME_UNSUPPORTED",
  "trace_id":    "01J9..."
}
```

- `error_code` is a stable `SCREAMING_SNAKE_CASE` string clients may switch on; HTTP status is for transport semantics only.
- `trace_id` echoes the OTEL trace id when present.
- 422 responses additionally include `errors: [{field, message}, …]` for field-level validation (e.g. missing `source_id`).
- **`/livez`, `/readyz`, `/metrics` are the only endpoints whose 2xx body is NOT problem+json**; their non-2xx still uses problem+json.

### 4.1.2 Error Code Catalog (I6)

Inventory of every `error_code` emitted by P1 (API responses + log events). New codes MUST be added here in the same commit that introduces them.

| `error_code` | HTTP / Surface | When | Origin |
|---|---|---|---|
| `INGEST_MIME_UNSUPPORTED`            | 415         | MIME outside the §4.2 P1 allow-list | Router T2.13 |
| `INGEST_FILE_TOO_LARGE`              | 413         | Multipart body > 50 MB | Router T2.13 |
| `INGEST_ARCHIVE_UNSAFE`              | 413 via `documents.error_code` | DOCX/PPTX zip preflight rejected the archive — `reason ∈ {invalid, members, ratio, expanded, per_member, traversal}` (T-SEC.3/.4) | Splitter T-SEC.4 |
| `INGEST_PDF_TOO_MANY_PAGES`          | 413 via `documents.error_code` | PDF page count exceeds `INGEST_MAX_PDF_PAGES` (T-SEC.5/.6) | Splitter T-SEC.6 |
| `INGEST_VALIDATION`                  | 422         | Missing/empty `source_id` / `source_app` / `source_title` (S23) — `errors[]` lists offending fields | Router T2.13 |
| `INGEST_MINIO_SITE_UNKNOWN`          | 422         | `minio_site` not in `MinioSiteRegistry` | Router T2.13 |
| `INGEST_OBJECT_NOT_FOUND`            | 422         | `(minio_site, object_key)` HEAD-probe miss | Router T2.13 |
| `INGEST_NOT_FOUND`                   | 404         | `GET /ingest/v1/{id}` / `DELETE /ingest/v1/{id}` / `POST /ingest/v1/{id}/rerun` on unknown id | Service T2.10 |
| `INGEST_NOT_RERUNNABLE`              | 409         | `POST /ingest/v1/{id}/rerun` on a document whose status is `READY` or `DELETING` (re-POST is the supersede path for READY; DELETING is mid-cascade) | Router (rerun endpoint) |
| `CHAT_MESSAGES_MISSING`              | 422         | `messages` absent or empty | Schema T3.3 |
| `CHAT_PROVIDER_UNSUPPORTED`          | 422         | `provider` outside `{"openai"}` allow-list (B22) | Schema T3.3 |
| `CHAT_FILTER_INVALID`                | 422         | `source_app` empty / > 64 chars, or `source_meta` empty / > 1024 chars (B29 → B35) | Schema T3.3 |
| `CHAT_RATE_LIMITED`                  | 429 + `Retry-After` | Per-user fixed-window quota exceeded on `/chat/v1` or `/chat/v1/stream` (B31, S37) | Router-level Depends T3.16 |
| `FEEDBACK_TOKEN_INVALID`             | 401         | HMAC mismatch, malformed token, or `shown_source_ids` doesn't match the signed `sources_hash` (T-FB.6, B55) | Router (feedback) |
| `FEEDBACK_TOKEN_EXPIRED`             | 410         | Token `ts` outside the 7-day window (T-FB.6, B55) | Router (feedback) |
| `FEEDBACK_SOURCE_INVALID`            | 422         | `source_id ∉ shown_source_ids` (T-FB.6) | Router (feedback) |
| `FEEDBACK_VALIDATION`                | 422         | Schema violations: vote ∉ {±1}, reason outside B56 enum, missing required field | Schema (feedback) |
| `CHAT_LLM_ERROR`                     | 502 / SSE-error | Pre-stream LLM failure (problem+json) or mid-stream LLM failure (`data: {type:error}`, B6) | Router T3.10/T3.12 |
| `CHAT_RETRIEVER_ERROR`               | 502 / SSE-error | ES vector / BM25 retriever failure | Router T3.10/T3.12 |
| `MCP_PARSE_ERROR`                    | JSON-RPC `-32700` | Request body is not valid JSON (S64) | Router P2.5 |
| `MCP_INVALID_REQUEST`                | JSON-RPC `-32600` | Missing `jsonrpc:"2.0"` / `method`; malformed envelope | Router P2.5 |
| `MCP_METHOD_NOT_FOUND`               | JSON-RPC `-32601` | Method outside §3.8.2 allow-list (S61) | Router P2.5 |
| `MCP_TOOL_NOT_FOUND`                 | JSON-RPC `-32602` (data.error_code) | `tools/call` with unknown `name` (S62) | Router P2.5 |
| `MCP_TOOL_INPUT_INVALID`             | JSON-RPC `-32602` (data.error_code) | `tools/call` arguments fail `inputSchema` validation (S63) | Router P2.5 |
| `MCP_TOOL_EXECUTION_FAILED`          | JSON-RPC `-32001` (data.error_code) | Underlying retrieval pipeline raises (S67) | Router P2.5 |
| `ES_PLUGIN_MISSING`                  | 503 (`/readyz`) | ES cluster missing `analysis-icu` plugin (B26, T0.8g) | Bootstrap / readyz |
| `ES_INDEX_MISSING`                   | 503 (`/readyz`) | A `resources/es/*.json` index is absent at boot | Bootstrap / readyz |
| `SCHEMA_DRIFT`                       | 503 (`/readyz`) + log `event=schema.drift` | Live schema differs from `schema.sql` / `resources/es/` | Bootstrap |
| `PIPELINE_TIMEOUT`                   | log `event=ingest.failed reason=pipeline_timeout` | Pipeline body exceeds `PIPELINE_TIMEOUT_SECONDS` (B18, S34) | Worker T3.2j |
| `ES_BBQ_UNSUPPORTED`                 | log `event=es.bbq_unsupported` | Cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW (B26) | Bootstrap |
| `RECONCILER_TICK_MISSING`            | Prometheus alert | `reconciler_tick_total` flat > 10 min (R8, S30) | Alerting rule T7.1a |
| `AUTH_TOKEN_EXPIRED`                 | 401             | JWT `exp` claim is in the past (raised through joserfc's verification path, T8.1a) | Auth middleware T8.2a |
| `AUTH_CLAIM_MISSING`                 | 401             | `<RAGENT_JWT_CLAIM_USER_ID>` claim absent or empty after JWKS verification (T8.1a) | Auth middleware T8.2a |
| `AUTH_TOKEN_INVALID`                 | 401             | JWT header absent, token malformed, signature mismatch, wrong `iss`, wrong `aud`, or any other JWKS verification failure outside expiry/missing-claim (T8.1a) | Auth middleware T8.2a |

### 4.2 Supported Formats

| Format | Converter | MIME (allow-list) | Notes | Phase |
|---|---|---|---|:---:|
| `.txt`  | `TextFileToDocument`     | `text/plain`              | UTF-8 text | **P1** |
| `.md`   | `MarkdownToDocument`     | `text/markdown`           | front-matter stripped | **P1** |
| `.html` | `HTMLToDocument`         | `text/html`               | visible text, script/style stripped | **P1** |
| `.pdf`  | `_PdfASTSplitter`        | `application/pdf`         | per-page `pymupdf4llm.to_markdown` → `_MarkdownASTSplitter`; RapidOCR auto-selected for image-bearing pages; structured atoms (headings, tables, paragraphs); `INGEST_PDF_MARGIN_PTS` clips header/footer zones | **P1** |
| `.docx` | `_DocxASTSplitter`       | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | paragraphs + tables (python-docx) | **P1** |
| `.pptx` | `_PptxASTSplitter`       | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | one atom per slide (python-pptx); footer/date/slide-number placeholders excluded | **P1** |
| `.xlsx` | `XLSXToDocument`         | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | active sheets | P2 |

> 415 on unsupported MIME; 413 on > 50 MB. PDF: `pymupdf4llm.to_markdown` per page; `rapidocr-onnxruntime` auto-selected for image-bearing pages; fallback to `page.get_text("text")` on error. `INGEST_PDF_MARGIN_PTS` (default `0`) clips header/footer zones. PPTX footer/date/slide-number placeholders always excluded.

### 4.3 Pipeline Catalog

| Pipeline | Components | Timeouts | Test Path | Phase |
|---|---|---|---|:---:|
| **Ingest** | `delete_by_document_id (idempotency) → FileTypeRouter → Converter → DocumentCleaner → LanguageRouter → {cjk_splitter \| en_splitter} (sentence-level, B1) → EmbeddingClient(bge-m3, batch=32) → ChunkRepository.bulk_insert → PluginRegistry.fan_out (per-plugin 60 s)` | Embedder 30 s/batch · ES bulk 60 s · MinIO get 30 s · plugin 60 s | `tests/integration/test_ingest_pipeline.py` | **P1** sync |
| **Chat** | `QueryEmbedder → ESVector(kNN on `embedding`, `bbq_hnsw` index, optional `term` filter on `source_app`/`source_meta` — B29 → B35) → ESBM25(multi_match `text`+`title^2`, `icu_text` analyzer, B26, same optional filter) → DocumentJoiner (C6 `CHAT_JOIN_MODE`: rrf\|concatenate\|vector_only\|bm25_only) → SourceHydrator(JOIN documents → returns full chunk content) → LLMClient.{chat\|stream}` (retrievers sequential in P1; parallel in P2 — see §3.4 P-A); router truncates `sources[].excerpt` to `EXCERPT_MAX_CHARS` (B23) | Embedder 10 s (single query) · ES query 10 s · LLM 120 s · per-batch ingest embed 30 s (asymmetric — query is one string, ingest is up to 32) | `tests/integration/test_chat_endpoint.py` (T3.9), `tests/integration/test_chat_stream_endpoint.py` (T3.11), `tests/integration/test_chat_pipeline_retrieval.py` (T3.5) | **P1** sync |
| **Retrieve** | Same as Chat pipeline up to `SourceHydrator` (shared `retrieval_pipeline` instance); no LLM call; router truncates `chunks[].excerpt` to `EXCERPT_MAX_CHARS` (B23); optional `dedupe` post-step (§3.4.4) | Embedder 10 s · ES query 10 s | `tests/unit/test_retrieve_router.py` (T3.19) | **P1** sync |

### 4.4 Plugin Catalog

| Plugin | `name` | `required` | `queue` | `extract()` | `delete()` | Phase |
|---|---|:---:|---|---|---|:---:|
| `VectorExtractor`    | `vector`     | ✓ | `extract.vector` | embed `f"{source_title}\n\n{chunk_text}"` (B15) → ES bulk index by `chunk_id`, denormalising `title`, `source_app`, `source_meta` onto each row (B15, B29 → B35) | ES bulk `_op_type=delete` | **P1** |
| `StubGraphExtractor` | `graph_stub` | — | `extract.graph`  | no-op | no-op | **P1** |
| `GraphExtractor`     | `graph`      | — | `extract.graph`  | LightRAG → Graph DB upsert | entity GC + ref_count | P3 |

### 4.5 Third-Party Client Catalog

| Client | Endpoint | Auth | Phase |
|---|---|---|:---:|
| `TokenManager` (×3 local / ×1 K8s) | `AI_API_AUTH_URL/auth/api/accesstoken` | J1 `{"key":…}` → J2 | **P1** |
| `EmbeddingClient` | `EMBEDDING_API_URL/text_embedding`              | J2 | **P1** |
| `LLMClient`       | `LLM_API_URL/gpt_oss_120b/v1/chat/completions` | J2 | **P1** |
| `RerankClient`    | `RERANK_API_URL/`                               | J2 | P1 unit / P2 wired |
| `HRClient`        | `HR_API_URL/v3/employees`                       | `Authorization` | P2 |

All 3rd-party calls: timeout/retry/backoff per `00_rule.md`; circuit-breaker on client.

**TokenManager refresh discipline (P-F):** per-instance `threading.Lock`; single-flight refresh at `expiresAt − 5 min`. Local: 3 independent managers (one per J1 token). K8s mode (`AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true`): one shared manager, SA token file, J2 shared across all clients.

### 4.6 Environment Variables (C2 + B28)

> **Inventory rules (B28):** every external dependency, every per-call timeout, every operational threshold, and every credential MUST appear in this table. Code that reads a literal value not represented here is a spec drift bug. Vars marked `(required)` have no default and refuse boot.

> **v2 removed vars (C6):** `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/SECURE/BUCKET` (→ `MINIO_SITES`), `INGEST_MAX_FILE_SIZE_BYTES` (→ `INGEST_INLINE/FILE_MAX_BYTES`), `CHUNK_TARGET_CHARS_EN/CJK/CSV`, `CHUNK_OVERLAP_CHARS_EN/CJK/CSV`, `CHUNK_HARD_SPLIT_OVERLAP_CHARS`.

#### 4.6.1 Bootstrap & HTTP server

| Variable | Default | Description |
|---|---|---|
| `RAGENT_ENV`                          | (required)       | `dev` \| `staging` \| `prod`. Modes A & B require `dev`; Mode C tolerates any value (§1). |
| `RAGENT_AUTH_DISABLED`                | `false`          | `true` selects Mode A (open auth — guard requires dev + loopback). `false` selects Mode B or C depending on `RAGENT_TRUST_X_USER_ID_HEADER` (§1, §3.5). |
| `RAGENT_TRUST_X_USER_ID_HEADER`       | `false`          | When `true` (and `AUTH_DISABLED=false`), selects Mode B: JWT middleware bypassed, `<RAGENT_USER_ID_HEADER>` trusted as `user_id` (§3.5). Guard requires `RAGENT_ENV=dev`. |
| `RAGENT_USER_ID_HEADER`               | `X-User-Id`      | Canonical header name for the downstream `user_id`. In trust mode: inbound header read directly. In JWT mode: extracted claim injected into this header on request scope. Currently hard-coded in router aliases — changing requires updating each alias. `RequestLoggingMiddleware` reads from `request.scope["ragent.user_id"]` so logging is header-name-agnostic. |
| `RAGENT_JWT_HEADER`                   | `X-Auth-Token`   | **Mode C only.** Inbound header carrying the raw JWT (no `Bearer ` prefix). Read only when `RAGENT_AUTH_DISABLED=false` AND `RAGENT_TRUST_X_USER_ID_HEADER=false`. |
| `RAGENT_JWT_CLAIM_USER_ID`            | `preferred_username` | **Mode C only.** JWT payload claim path used as the downstream `user_id`. Verified value is non-empty string; missing/empty → 401 `AUTH_CLAIM_MISSING`. |
| `OIDC_DOMAIN`                         | (required in Mode C) | OIDC issuer domain. JWKS is fetched from `{scheme}://<OIDC_DOMAIN>/.well-known/jwks.json` (resolved via the OIDC discovery `jwks_uri`); the verifier validates `iss == discovery["issuer"]`. Guard exits if unset when `AUTH_DISABLED=false` AND `TRUST_X_USER_ID_HEADER=false`. |
| `OIDC_AUDIENCE`                       | (required in Mode C) | Expected `aud` claim. Tokens with mismatched `aud` → 401 `AUTH_TOKEN_INVALID`. |
| `OIDC_USE_HTTPS`                      | `true`           | Scheme toggle for the OIDC discovery + JWKS URL. Set `false` ONLY for in-cluster discovery or local fixture; production deployments MUST keep `true`. |
| `OIDC_VERIFY_SSL`                     | `true`           | Verify the IdP's TLS certificate during OIDC discovery + JWKS fetch. Set `false` ONLY for dev/staging against self-signed Keycloak. For production with a private CA, leave `true` and mount the CA via `SSL_CERT_FILE` instead. |
| `RAGENT_PERMISSION_INGEST_ENABLED`    | `false`          | **P2 only.** When `true`, `GET/DELETE /ingest/v1/{id}` and `GET /ingest/v1` enforce `PermissionClient` (§3.5). Default off — gate is wired but inert until OpenFGA tuples exist. |
| `RAGENT_PERMISSION_CHAT_ENABLED`      | `false`          | **P2 only.** When `true`, chat retrieval applies the `PermissionClient` post-filter (§3.5). Default off. |
| `RAGENT_HOST`                         | `127.0.0.1`      | API bind address. Guard (§1) refuses any value other than `127.0.0.1` in Mode A (open auth — no auth surface, must bind loopback). Modes B/C tolerate any bind. |
| `RAGENT_PORT`                         | `8000`           | API bind port. |
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
| `MINIO_SITES`                         | (required)       | v2: JSON list of `{name, endpoint, access_key, secret_key, bucket, secure?, read_only?}`. Must include `name="__default__"` (inline ingest). Supersedes the legacy `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/SECURE/BUCKET` vars (removed). |

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
| `RETRIEVAL_MIN_SCORE`                 | *(unset)*        | Global score floor; null = no filtering. Must be >= 0.0 if set. |
| `EXCERPT_MAX_CHARS`                   | `512`            | `_ExcerptTruncator` truncation length (B23). |
| `RAGENT_DEFAULT_LLM_PROVIDER`         | `openai`         | Echoed when request omits `provider`. |
| `RAGENT_DEFAULT_LLM_MODEL`            | `gptoss-120b`    | Echoed when request omits `model`. |
| `RAGENT_DEFAULT_LLM_TEMPERATURE`      | `0.7`            | |
| `RAGENT_DEFAULT_LLM_MAX_TOKENS`       | `4096`           | |
| `RAGENT_DEFAULT_SYSTEM_PROMPT`        | `You are a helpful assistant` | Auto-prepended when `messages` lacks a `system` entry. |
| `RAGENT_DEFAULT_RAG_SYSTEM_PROMPT`    | *(multi-intent template)*     | Multi-intent template; injected when retrieval returns ≥1 doc and caller has no system message. |
| `RAGENT_RAG_GROUNDING_RULES`          | *(rules-only variant)*        | Rules-only variant; prepended when caller supplies own system message with retrieved docs. |
| `CHAT_RATE_LIMIT_PER_MINUTE`          | `30`             | Per-user request cap on `/chat/v1` + `/chat/v1/stream` within the rate-limit window (B31). Excess returns 429 `CHAT_RATE_LIMITED`. |
| `CHAT_RATE_LIMIT_WINDOW_SECONDS`      | `60`             | Fixed-window length for `CHAT_RATE_LIMIT_PER_MINUTE` (B31). |
| `MCP_REQUEST_MAX_BYTES`               | `262144` (256 KiB) | Defence-in-depth cap on `POST /mcp/v1` request bodies; over-limit returns HTTP 413 `application/problem+json` (§3.8.1). |
| `CHAT_FEEDBACK_ENABLED`               | `false`          | Master switch (B54). When true: enables `/feedback/v1`, `_FeedbackMemoryRetriever`, requires `FEEDBACK_HMAC_SECRET`. Default off (B57). |
| `CHAT_FEEDBACK_RRF_WEIGHT`            | `0.5`            | Weight on feedback retriever in `DocumentJoiner(weights=[1.0, 1.0, this])` (B54). |
| `CHAT_FEEDBACK_MIN_VOTES`             | `3`              | `(likes + dislikes)` threshold below which a (source_app, source_id) is dropped from the retriever (B54). Defeats single-user signal poisoning. |
| `CHAT_FEEDBACK_HALF_LIFE_DAYS`        | `14`             | Score decay half-life applied to the per-source Wilson score: `score × 0.5 ** (age_days / this)` (B54). |
| `FEEDBACK_HMAC_SECRET`                | *(required when `CHAT_FEEDBACK_ENABLED=true`)* | HMAC key for `/feedback/v1` token sign/verify (B55). Boot fails when feedback enabled but unset. |

> **MCP protocol pins are NOT env-driven** — `protocolVersion` (`2024-11-05`) and `serverInfo.name` (`ragent`) are **pinned in spec §3.8.1 / B47** and live as module-level constants in `src/ragent/routers/mcp.py`. Operators flipping the protocol version would silently break the contract; the pin is intentional. The only MCP env knob is the body cap above.

#### 4.6.7 Per-call timeouts (matches §4.3 catalog)

| Variable | Default (s) | Site |
|---|---|---|
| `EMBEDDER_INGEST_TIMEOUT_SECONDS`     | `30`             | per-batch (32 strings) ingest call. |
| `EMBEDDER_QUERY_TIMEOUT_SECONDS`      | `10`             | single-string chat-query call (C8 asymmetric). |
| `ES_BULK_TIMEOUT_SECONDS`             | `60`             | `VectorExtractor` bulk index/delete. |
| `ES_QUERY_TIMEOUT_SECONDS`            | `10`             | chat retrievers (vector + BM25). |
| `MINIO_GET_TIMEOUT_SECONDS`           | `30`             | worker download from staging. |
| `MINIO_PUT_TIMEOUT_SECONDS`           | `60`             | router upload to staging. |
| `LLM_TIMEOUT_SECONDS`                 | `120`            | `LLMClient.{chat\|stream}`. |
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

---

## 5. Data Structures

### 5.1 MariaDB

> **v2 OVERRIDE** — `documents` adds `ingest_type ENUM('inline','file','upload') NOT NULL DEFAULT 'inline'`, `minio_site VARCHAR(64) NULL`, `source_url VARCHAR(2048) NULL`. The **`chunks` table is dropped** — chunks live only in ES `chunks_v1`. `object_key` semantics: for `inline`/`upload` it points into `__default__` MinIO site; for `file` it is the caller-supplied key in the named site (no copy). The third discriminator value `upload` was added by `migrations/011_ingest_type_upload.sql` to distinguish the multipart `POST /ingest/v1/upload` entry path from the JSON-body `inline` shape (different cleanup contract — see §3.1 table).

```sql
CREATE TABLE documents (
  document_id      CHAR(26)     PRIMARY KEY,
  create_user      VARCHAR(64)  NOT NULL,
  source_id        VARCHAR(128) NOT NULL,
  source_app       VARCHAR(64)  NOT NULL,
  source_title     VARCHAR(256) NOT NULL,
  source_meta      VARCHAR(1024) NULL,
  ingest_type      ENUM('inline','file','upload')  NOT NULL DEFAULT 'inline',
  minio_site       VARCHAR(64)   NULL,          -- NULL for inline (uses __default__ site)
  source_url       VARCHAR(2048) NULL,
  mime_type        VARCHAR(256)  NOT NULL,
  object_key       VARCHAR(256) NOT NULL,  -- MinIO key only (B10 format); bucket is config-driven (`MINIO_BUCKET`), not stored per-row (C3).
  status           ENUM('UPLOADED','PENDING','READY','FAILED','DELETING') NOT NULL,
  attempt          INT          NOT NULL DEFAULT 0,
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  INDEX idx_status_updated (status, updated_at),
  INDEX idx_source_app_id_status_created (source_app, source_id, status, created_at),
  INDEX idx_create_user_document (create_user, document_id)
);
-- (source_id, source_app) is the LOGICAL identity; uniqueness is eventual (supersede task), NOT a UNIQUE constraint.
-- create_user = audit only (NOT an authz field); idx_create_user_document supports "list my uploads" queries.

-- chunks table dropped in migration 003_drop_chunks.sql (v2). Chunks live in ES chunks_v1 only.

CREATE TABLE feedback (
  feedback_id     CHAR(26)     PRIMARY KEY,             -- new_id() (§5.3)
  request_id      CHAR(26)     NOT NULL,                -- echoed from /chat response
  user_id         VARCHAR(64)  NOT NULL,                -- X-User-Id at /chat time (signed into token)
  source_app      VARCHAR(64)  NOT NULL,                -- voted-source namespace (paired with source_id, B11/B35)
  source_id       VARCHAR(128) NOT NULL,                -- voted source (must be in shown_sources)
  vote            TINYINT      NOT NULL,                -- +1 like / -1 dislike
  reason          VARCHAR(32)  NULL,                    -- B56 enum (6 values) or NULL
  position_shown  SMALLINT     NULL,                    -- for future IPS (B57 item 1) — collected, unused in P1
  created_at      DATETIME(6)  NOT NULL,
  updated_at      DATETIME(6)  NOT NULL,
  UNIQUE KEY uq_user_req_app_src (user_id, request_id, source_app, source_id),
  CONSTRAINT ck_vote_unit CHECK (vote IN (-1, 1))
);
-- Append-only event log. ES `feedback_v1` (§5.4) is the derived serving view (B54/B55).
-- No content/text — query_text lives only in `feedback_v1` per the "text in ES, meta in MariaDB" rule.
```

No physical FK. ORM-level cascade only.

**ID classification:** Internal (`document_id`, `chunk_id`) — `CHAR(26)` UUIDv7→Crockford Base32 via `new_id()`. External (client-supplied): `source_id` ≤128, `source_app` ≤64, `source_title` ≤256, `source_meta` ≤1024, `create_user` ≤64 (audit only, not authz). `task_id` from `POST /ingest` is the `document_id`.

### 5.2 Elasticsearch `chunks_v1`

> **v2 OVERRIDE** — adds `raw_content` (`text, index: false`): original byte slice from splitter; LLM context and `sources[].excerpt` use `raw_content` (fallback to `content`). `source_url` added as `keyword`. `content` = normalized, BM25-analyzed.
>
> **Source of truth (B26):** `resources/es/chunks_v1.json` — bootstrap PUT-s if absent (§6.1); spec mirrors file; drift = CI failure (`test_es_resource_drift.py`).

```json
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "default_pipeline": "chunks_default",
      "analysis": {
        "analyzer": {
          "icu_text": {
            "type": "custom",
            "tokenizer": "icu_tokenizer",
            "filter": ["icu_folding", "lowercase"]
          }
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "chunk_id":         { "type": "keyword" },
      "document_id":      { "type": "keyword" },
      "source_app":       { "type": "keyword" },
      "source_meta":      { "type": "keyword", "ignore_above": 1024 },
      "source_url":       { "type": "keyword" },
      "lang":             { "type": "keyword" },
      "title":            { "type": "text", "analyzer": "icu_text" },
      "text":             { "type": "text", "analyzer": "icu_text" },
      "raw_content":      { "type": "text", "index": false },
      "indexed_at":       { "type": "date" },
      "embedding": {
        "type": "dense_vector",
        "dims": 1024,
        "index": true,
        "similarity": "cosine",
        "index_options": { "type": "bbq_hnsw" }
      }
    }
  }
}
```

**Topology (B26):** shards=1, replicas=0 (P1); prod overrides via cluster template. **BM25 (B26):** `icu_text` analyzer on `text`/`title`; `analysis-icu` hard dependency (verified `/readyz`); test override (`RAGENT_ES_RESOURCES_DIR`) uses `standard` (B42). **Vector (B58):** `bbq_hnsw` (~32× heap); test `chunks_v1.json` keeps `flat`. **Pipeline `chunks_default` (B59):** `init_es()` PUTs `resources/es/pipelines/chunks_default.json` (`{"processors":[{"set":{"field":"indexed_at","value":"{{{_ingest.timestamp}}}"}}]}`) before index; `indexed_at` = last write. **Title (B15):** `title` from `source_title`; BM25 `multi_match ["text","title^2"]` + semantic `embed(f"{source_title}\n\n{chunk_text}")`. **Filter (B29→B35):** `source_app`/`source_meta` as `keyword` ES terms (AND when both; scope metadata, not auth).

### 5.3 ID / DateTime

- `new_id()` → UUIDv7 → Crockford Base32 → 26 chars (lexicographically sortable).
- `utcnow()` → tz-aware UTC. `to_iso()` → ISO 8601 `...Z`. `from_db(naive)` → attach UTC.

### 5.4 Elasticsearch `feedback_v1`

> **Source of truth (B26 pattern):** `resources/es/feedback_v1.json` — settings + mappings, checked into git. Bootstrap (§6.1) reads this file and `PUT /feedback_v1` if the index does not exist. Mirrors the `chunks_v1` pattern (§5.2): ES holds text + vector, MariaDB holds meta only.

```json
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "analysis": {
        "analyzer": {
          "icu_text": { "type": "custom", "tokenizer": "icu_tokenizer",
                        "filter": ["icu_folding", "lowercase"] }
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "request_id":      { "type": "keyword" },
      "query_text":      { "type": "text",  "analyzer": "icu_text" },
      "query_embedding": { "type": "dense_vector", "dims": 1024,
                           "index": true, "similarity": "cosine",
                           "index_options": { "type": "flat" } },
      "source_id":       { "type": "keyword" },
      "source_app":      { "type": "keyword" },
      "source_meta":     { "type": "keyword", "ignore_above": 1024 },
      "vote":            { "type": "byte" },
      "reason":          { "type": "keyword" },
      "user_id_hash":    { "type": "keyword" },
      "ts":              { "type": "date" }
    }
  }
}
```

**Indexing:** `_id = sha256(user_id|request_id|source_app|source_id)` (idempotent re-vote overwrites). `query_embedding` via `EmbeddingClient.embed([query_text], query=True)`. `user_id_hash = sha256(user_id)` — plaintext `user_id` in MariaDB only.

---

## 6. Standards

- **Layers:** Router (HTTP only) → Service (orchestration) → Repository (CRUD only).
- **Methods:** ≤ 30 LOC, max 2-level nesting. Utilities in `utility/`.
- **IDs:** UUIDv7 + Crockford Base32 (26 chars). **DateTime:** end-to-end UTC + `Z` suffix.
- **DB:** no physical FK; index every `WHERE / JOIN / ORDER BY` field.
- **Quality gate:** `uv run ruff format . && uv run ruff check . --fix && uv run pytest --cov=src/ragent --cov-branch --cov-fail-under=92` before every commit. **Test coverage floor: 92% (line + branch)** — CI rejects drops; DoD requirement.
- **TDD commits:** `[STRUCTURAL]` or `[BEHAVIORAL]` prefix; never mixed.
- **JSON naming convention (B21):** within request/response bodies, **identifier and resource fields are `snake_case`** (`document_id`, `source_id`, `source_app`, `source_title`, `error_code`, `next_cursor`, `task_id`, `trace_id`); **LLM token/config knobs are `camelCase`** (`maxTokens`, `promptTokens`, `completionTokens`, `totalTokens`, `temperature`, `topP` if added later) — preserved to match upstream OpenAI-shape expectations. Within a single body both styles may coexist; the rule above resolves which to use for any new field.

### 6.1 Schema & Migration (B3)

Two artefacts, **both versioned in git**, both consulted at boot:

| Artefact | Path | Purpose | Owner |
|---|---|---|---|
| **Consolidated snapshot** | `migrations/schema.sql` | Single-file DDL representing the current target schema. Updated **in lockstep with every incremental migration**. Used by fresh dev/CI/testcontainers bring-up (`mariadb < schema.sql` → instant ready). | Dev |
| **Incremental migrations** | `migrations/NNN_<slug>.sql` (e.g. `001_initial.sql`, `002_add_workspace.sql`) | Forward-only ALTER scripts applied via Alembic (`alembic upgrade head`). Production / staging path. | Dev |

**Boot-time auto-init (idempotent):** `CREATE TABLE/INDEX IF NOT EXISTS` from `schema.sql`; `PUT /<index>` from `resources/es/<index>.json` if absent. No `ALTER`, no ES mapping update — drift → `event=schema.drift` + `/readyz` degraded. Production uses Alembic (DB) or `PUT /<index>-vN` + reindex (ES).

**Invariant:** `schema.sql` ≡ replaying all migrations. CI: `tests/integration/test_schema_drift.py` (dual-path `mysqldump` diff).

### 6.2 Module Layout

> Canonical project tree. Every file is produced by exactly one Green/Structural plan row; no file is written outside this layout. Layered dependency rule: **routers → services → repositories**; **plugins / clients / storage / pipelines** are leaf concerns injected via the composition root (B30). **Only `bootstrap/composition.py` reads env vars** — every other module receives its config via constructor argument (B17, B30).

```
ragent/
├── pyproject.toml
├── .env.example                                  # T0.11 (B30) — operator-facing config artifact
├── Dockerfile.es-test                            # T0.9  — ES container with analysis-icu pre-installed
├── deploy/k8s/reconciler-cronjob.yaml            # T5.2  (B9) — Reconciler CronJob manifest
├── migrations/
│   ├── schema.sql                                # T0.8a (B3) — consolidated snapshot
│   └── 001_initial.sql                           # T0.8  (B3) — forward-only Alembic
├── resources/es/chunks_v1.json                   # T0.8e (B26) — ES index source of truth
├── src/ragent/
│   ├── api.py                                    # T7.5d — `python -m ragent.api`     (uvicorn launcher)
│   ├── worker.py                                 # T7.5e — `python -m ragent.worker`  (TaskIQ launcher)
│   ├── reconciler.py                             # T5.2  — `python -m ragent.reconciler` (one-shot, B9)
│   ├── bootstrap/
│   │   ├── guard.py                              # T7.5  — RAGENT_ENV/AUTH/HOST/LOG_LEVEL guard
│   │   ├── broker.py                             # T0.10 (B27/B30) — TaskIQ broker; sole `@broker.task` import
│   │   ├── composition.py                        # T7.5a (B30) — composition root / DI Container; sole env-reader
│   │   ├── init_schema.py                        # T0.8d (B3, B26) — CREATE IF NOT EXISTS / PUT index
│   │   └── app.py                                # T7.5c — FastAPI `create_app()` + lifespan auto-init
│   ├── routers/
│   │   ├── ingest.py                             # T2.14 (B5) — /ingest CRUD + RFC 9457
│   │   ├── chat.py                               # T3.10/T3.12 (B12, B6) — /chat + /chat/stream
│   │   ├── mcp.py                                # T-MCP.* — /mcp/v1 JSON-RPC 2.0 server (§3.8)
│   │   └── health.py                             # T7.8  (B4, C9) — /livez /readyz /metrics
│   ├── services/ingest_service.py                # T2.8 / T2.10 / T2.12 / T3.2d — create / delete / list / supersede
│   ├── repositories/
│   │   ├── document_repository.py                # T2.2  (B11/B14/B16/B25/B29) — CRUD + heartbeat + supersede helpers
│   │   └── chunk_repository.py                   # T2.4
│   ├── plugins/
│   │   ├── protocol.py                           # T1.2  — `ExtractorPlugin` Protocol (frozen, §3.3)
│   │   ├── registry.py                           # T1.7  — `PluginRegistry`, fan_out, per-plugin timeout
│   │   ├── vector.py                             # T1.10 / T1.12 (B15/B17/B29) — VectorExtractor (DI)
│   │   └── stub_graph.py                         # T1.4  — no-op P1 placeholder for §4.4 graph row
│   ├── pipelines/
│   │   ├── factory.py                            # T3.2 / T3.5a — ingest + chat factories (CHAT_JOIN_MODE dispatch)
│   │   ├── ingest.py                             # T3.2  (B1) — Haystack components + AST splitters
│   │   └── chat.py                               # T3.6  (B23) — `build_retrieval_pipeline` + SourceHydrator
│   ├── clients/
│   │   ├── auth.py                               # T4.2  (P-F, S9) — TokenManager (J1→J2, single-flight)
│   │   ├── embedding.py                          # T4.4  (C8) — bge-m3, batched, asymmetric timeouts
│   │   ├── llm.py                                # T4.6 / T3.8 (B12) — chat + stream
│   │   ├── rerank.py                             # T4.8  — P1 unit only, P2 wired
│   │   └── rate_limiter.py                       # T3.14 (B31) — Redis fixed-window per-key counter; powers chat /chat/stream Depends
│   ├── storage/minio_client.py                   # T2.6  (B10/B25/B28) — key-only return; bucket from MINIO_BUCKET
│   ├── workers/                                  # @broker.task modules — auto-imported by worker.py
│   │   ├── ingest.py                             # T3.2b (B16/B18) — `ingest.pipeline` task
│   │   └── supersede.py                          # T3.2d (P-C) — `ingest.supersede` task
│   ├── schemas/chat.py                           # T3.4  (B12/B21/B22/B29) — Pydantic ChatRequest
│   ├── errors/problem.py                         # T2.14 (B5) — RFC 9457 builder + error_code (§4.1.2)
│   ├── utility/
│   │   ├── id_gen.py                             # T0.4  — UUIDv7 → Crockford base32 (26 char)
│   │   └── datetime.py                           # T0.6  — UTC + ISO-Z helpers
│   └── state_machine.py                          # T0.7 (S10) — status transition rules; consumed by repo.update_status
└── tests/{conftest.py, unit/, integration/, e2e/}
```

---

## 7. Decision Log

> Frozen 2026-05-04. Each row records a once-blocking design choice with the alternatives considered. Changes require a new dated row (append-only, never edit in place).

| ID | Date | Domain | Question | Decision | Alternatives rejected | Affects |
|---|---|---|---|---|---|---|
| **B1** | 2026-05-04 | NLP | Chinese chunking strategy in `LanguageRouter` | **Sentence-level split** with `en_splitter` (default) and `cjk_splitter` (CJK branch). Both emit one chunk per sentence; downstream embedder batches them (32/call). | jieba word-segmentation (heavyweight, P3 graph concern); omit CN in P1 (kills demo). | §3.2 / §4.3 / T3.1 |
| **B3** | 2026-05-04 | DB | Migration tool | **Both:** `migrations/schema.sql` (consolidated snapshot, kept current) + `migrations/NNN_*.sql` (Alembic-applied incrementals). Boot performs idempotent `CREATE … IF NOT EXISTS` for MariaDB tables/indexes and ES indexes; never `ALTER`. | Alembic-only (no quick CI bring-up); raw-only (no audit trail of changes); sqlx-style (Python toolchain mismatch). | §6.1 / T0.8 |
| **B4** | 2026-05-04 | Ops | Health/metrics endpoints | **App layer:** `/livez`, `/readyz`, `/metrics`. K8s probes use `/livez` for liveness and `/readyz` for readiness; Prometheus scrapes `/metrics`. **Infra layer:** K8s pod-level liveness only (no in-app dep probes for liveness — would cause cascading restarts on transient ES blips). | Single `/health` endpoint (conflates liveness vs readiness); separate sidecar exporter (extra deploy unit). | §4.1 / T7.1 / T7.7 |
| **B5** | 2026-05-04 | API | REST error response shape | **RFC 9457 Problem Details** (`application/problem+json`) with extension `error_code` (stable `SCREAMING_SNAKE_CASE` business identifier). 422 also carries `errors[]` for field validation. | Bare `{error, message}` (no standard, no machine-readable code); RFC 7807 (superseded by 9457). | §4.1.1 / T2.13 |
| **B6** | 2026-05-04 | API/SSE | Mid-stream error contract on `/chat` | **`data:` line with payload `{type:"error", error_code, message}`**, then close. No `event: error` named-event — keeps client parser uniform (every line is JSON). Pre-stream errors use normal RFC 9457 response. | `event: error` named SSE event (forces dual parser path); silently truncate (loses error_code). | §3.4 / T3.3 |
| **B7** | 2026-05-04 | API | `GET /ingest?after=&limit=` semantics | **Cursor pagination by `document_id` DESC** (UUIDv7 → time-ordered, newest-first). `after` = last `document_id` of previous page; because ordering is DESC, next-page cursor uses `WHERE document_id < :after`; server returns `next_cursor` = last (oldest) id of current page. Optional exact-match filters `source_id` and `source_app` narrow results to a specific logical document or application without changing pagination semantics. | OFFSET-based (linear scan); page-number based (incompatible with cursor stability); keyset on `created_at` (collisions); ASC ordering (returns oldest first — poor UX for "show me my recent uploads"). | §4.1 / T2.11 |
| **B8** | 2026-05-04 | Test infra | Integration backends | **`testcontainers-python`** spins up MariaDB + ES + Redis + MinIO per integration session (module-scoped fixture; reused across tests). | docker-compose (manual, dev-only); in-process fakes (drift from prod behaviour). | T0.9 / all `tests/integration/` |
| **B9** | 2026-05-04 | Resilience | Reconciler scheduler | **Kubernetes `CronJob`** `*/5 * * * *` running `python -m ragent.reconciler` with `concurrencyPolicy: Forbid`. | TaskIQ scheduled task (broker outage = sweeper outage; sweeper is the recovery surface for broker outage); APScheduler (in-process, dies with worker pod). | §3.6 / T5.2 |
| **B10** | 2026-05-04 | Storage | MinIO object key format | **`{source_app}_{source_id}_{document_id}`** in a single bucket from `MINIO_BUCKET` env (default `ragent`). `source_app` and `source_id` sanitised to `[A-Za-z0-9._-]`. The `document_id` suffix preserves uniqueness during transient duplicates pre-supersede. | `{document_id}` only (loses source provenance for forensic / orphan-sweep tooling); `{owner}/{document_id}` (P1 OPEN has no owner); per-source bucket (bucket sprawl). | §3.1 / T2.5 / T2.6 |
| **B11** | 2026-05-04 | Ingest | Display-title surface for chat `sources[]` | **`source_title` mandatory** on `POST /ingest` (`VARCHAR(256) NOT NULL`). Joined into chat retrieval as `sources[].title`. 422 if missing/empty. | Derive from filename (lossy, ugly); store on chunk row (denormalised, redundant); make optional + fallback to `source_id` (degrades chat UX). | §3.1 / §4.1 / §5.1 / T2 |
| **B12** | 2026-05-04 | Chat API | Streaming vs non-streaming response | **Two endpoints:** `POST /chat` (synchronous JSON, §3.4.2 body) and `POST /chat/stream` (SSE; same body delivered as terminal `done` event after `delta` chunks). Shared §3.4.1 request schema with defaults (`provider="openai"`, `model="gptoss-120b"`, `temperature=0.7`, `maxTokens=4096`); auto-prepend default system message if absent. | Single SSE-only endpoint (forces streaming clients on simple integrations); single JSON-only endpoint (loses streaming UX); `Accept`-header content-negotiation on one path (subtle bugs, harder to test). | §3.4 / §4.1 / T3.3–T3.4 |
| **B13** | 2026-05-04 | Chat API | `sources[].type` taxonomy | **Reserved enum** `"knowledge" \| "app" \| "workspace"`; **P1 always emits `"knowledge"`**. Future phase derives `"app"` / `"workspace"` (likely from `source_app` / `source_workspace` semantics). | Drop the field for now (breaks forward-compat clients); ship full derivation logic in P1 (out of scope, no acceptance criteria). | §3.4.2 / T3.3 |
| **B14** | 2026-05-04 | Auth/Permission | (a) `documents.owner_user_id` semantics; (b) where ACL lives | **(a)** Rename to `create_user` — pure audit metadata recording the `X-User-Id` of the creating request, **not** an authorization field. **(b)** Authentication and Permission are separate layers. ES (`chunks_v1`) carries no auth fields in any phase. Permission gating runs **post-retrieval** via a `PermissionClient` Protocol; future-phase backend = **OpenFGA** (supersedes the earlier "out-of-scope across all phases" declaration). Index renamed `idx_owner_document` → `idx_create_user_document`. | Owner-based ES filter (couples auth to retrieval; re-index on every model change); keep "owner" naming with auth semantics (overloads the column, blocks future sharing/role models); keep OpenFGA out-of-scope (no scalable answer for sharing). | §1 / §3.4 / §3.5 / §4.1 / §5.1 / T0.8 / T2.1 / T8 |
| **B15** | 2026-05-04 | Retrieval | How `source_title` participates in chat retrieval | **Two surfaces, no extra retriever:** (1) **Semantic** — `VectorExtractor` embeds `f"{source_title}\n\n{chunk_text}"` at ingest, so the existing `embedding` already carries title semantics. (2) **Lexical** — `title` is denormalised onto each chunk row in `chunks_v1`; `ESBM25Retriever` runs `multi_match` on `["text", "title^2"]` (2× boost). Existing 2-retriever + RRF topology unchanged. | BM25-only on title (misses semantic matches like "meeting"→"sync notes"); separate `title_embedding` vector field + 3rd retriever (3-way RRF, extra ingest embed call, mapping bloat); join `documents.source_title` post-retrieval for ranking only (loses BM25 + vector influence on top-K selection). | §3.2 / §3.4 / §4.4 / §5.2 / T1.9 / T3.5 |
| **B16** | 2026-05-04 | Resilience | Worker–Reconciler concurrency safety | **Worker heartbeat:** during the pipeline body the worker updates `documents.updated_at = NOW()` every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30 s, single PK-keyed `UPDATE`). Reconciler's threshold becomes `updated_at < NOW() - 5 min` — a live worker is never re-dispatched. Closes the no-lock-window race opened by the §3.1 short-tx locking discipline. | Hold a row lock across pipeline body (defeats the §3.1 reform); add `assigned_to_worker` lease column (extra write per status mutation, lease-renewal complexity); rely on TaskIQ message-id deduplication (only catches redelivery, not Reconciler-initiated parallel kiq). | §3.1 / §3.6 / §4.6 / T2.1 / T3.2b |
| **B17** | 2026-05-04 | Plugin | How `VectorExtractor.extract(document_id)` reads `source_title` (Protocol cannot pass it as an arg) | **Constructor injection:** `VectorExtractor.__init__(repo, chunks, embedder, es)`. `extract()` calls `repo.get(document_id).source_title`. Protocol §3.3 stays frozen. Plugins are constructed by composition root with their dependencies and registered as instances. | Widen Protocol to `extract(document_id, metadata)` (breaks Protocol freeze, every plugin pays the metadata-dict cost forever); pass via Haystack channel input (couples plugin to pipeline assembly); fetch via global service-locator (hidden coupling). | §3.3 / §4.4 / T1.12 |
| **B18** | 2026-05-04 | Resilience | Per-document pipeline timeout | **Hard ceiling `PIPELINE_TIMEOUT_SECONDS` (default 1800 = 30 min)** around the worker pipeline body. Overrun ⇒ `FAILED` with `error_code=PIPELINE_TIMEOUT`, full cleanup. Bounds pathological inputs (runaway plugin, oversized document) deterministically; heartbeat catches faster (5 min) but timeout is the deterministic upper bound. | No ceiling (relies on heartbeat alone; allows worker pods to be tied up indefinitely on bad data); reject docs at upload time by estimated processing cost (estimation is unreliable). | §3.1 / §3.2 / §4.6 / S34 |
| **B21** | 2026-05-04 | API | JSON field naming convention | **IDs / resources = `snake_case`** (`document_id`, `source_id`, `error_code`, `next_cursor`, …); **LLM token/config knobs = `camelCase`** (`maxTokens`, `promptTokens`, `completionTokens`, `totalTokens`, `temperature`). Mixed within one body is allowed; the rule above resolves which side a new field falls on. Preserves OpenAI-shape upstream familiarity for chat tokens while keeping ingest/data fields snake-case. | All-snake (breaks user-specified chat shape); all-camel (forces `documentId`/`sourceId` rename across ingest, schema, OpenFGA tuples, audit logs); ad-hoc per field (was the bug). | §6 / all body schemas |
| **B22** | 2026-05-04 | Chat API | `provider` field semantics in P1 | **Validated allow-list `{"openai"}`**, 422 (`error_code=CHAT_PROVIDER_UNSUPPORTED`) on others; the accepted value is **echoed verbatim** in the response. P1 routes nothing on it. Future phases extend the allow-list and use `provider` as a routing key. | Echo only, no validation (silently accepts garbage); ignore the field entirely (forward-incompat with multi-provider future). | §3.4.1 / §3.4.2 / T3.3–T3.4 |
| **B23** | 2026-05-05 | Chat API | Where `sources[].excerpt` is truncated | **In the router** (`_build_sources` for `/chat`, `_to_chunk` for `/retrieve`), after retrieval — `EXCERPT_MAX_CHARS` (default 512) hard character cut. `SourceHydrator` returns full chunk content; the LLM receives the untruncated text; only the API response field is shortened. `EXCERPT_MAX_CHARS` is a public constant exported from `pipelines/retrieve.py` and imported by both routers. | Truncate inside `SourceHydrator` (LLM context is also cut — original P1 approach, reverted: reduces answer quality on long chunks without benefit to the API consumer); truncate in retriever (couples retrieval to display concerns); leave to client (full chunk surfaced to API consumer — bandwidth waste + potential text leakage). | §3.4.2 / §3.4.4 / T3.6 / T3.19 |
| **B25** | 2026-05-04 | Storage | `documents.storage_uri` stored full URI; bucket name is constant config | **Rename column to `object_key VARCHAR(256) NOT NULL`** (key only, format per B10). Bucket is read from `MINIO_BUCKET` env var (default `ragent`); reconstruct full URI on demand. Saves ~20 bytes/row, decouples row from bucket-rename ops, and makes a future bucket migration a config flip. | Keep full URI (rigid); store bucket per-row (rotation hell); URL-encode in object key (key+bucket separation already does the job). | §5.1 / T2.5 / T2.6 |
| **B26** | 2026-05-04 | ES | (a) BM25 analyzer; (b) vector index type; (c) where the index definition lives | **(a) `icu_text` custom analyzer** (`icu_tokenizer` + `icu_folding` + `lowercase`) on `text` and `title` — required for CJK tokenisation; `standard` analyzer collapses CJK to per-character or mega-tokens, breaking BM25. `analysis-icu` plugin is a hard ES dependency (verified at `/readyz`). **(b) `bbq_hnsw`** (Better Binary Quantization HNSW, ES 8.16+) on `embedding` — ~32× memory reduction at negligible recall cost; falls back to standard HNSW with `event=es.bbq_unsupported` log if cluster rejects. **(c) Source of truth = `resources/es/chunks_v1.json`** loaded by boot auto-init (§6.1) when the index does not exist; spec §5.2 mirrors the file in prose; CI drift test enforces equality. | Default `standard` analyzer (CJK becomes useless for BM25); `nori`/`smartcn` (per-language plugin sprawl, doesn't cover all CJK consistently); raw HNSW (4× more memory at our 1024 dims); inline mapping in Python code (every change is a code commit, no resource-file diffability). | §5.2 / §6.1 / T0.8d / T0.9 |
| **B27** | 2026-05-04 | Infra | Redis topology — single-instance vs Sentinel HA | **Per-instance toggle via `REDIS_MODE` env (`standalone` \| `sentinel`)**. Both broker and rate-limiter share the mode; standalone reads `REDIS_BROKER_URL` / `REDIS_RATELIMIT_URL`; sentinel reads `REDIS_SENTINEL_HOSTS` (shared quorum) + `REDIS_*_SENTINEL_MASTER` (per-instance master name). Connection layer dispatches on mode (`redis-py-sentinel` vs `redis-py`). Default `standalone` for dev/CI; prod sets `sentinel`. | Hardcode Sentinel (broken local dev); hardcode standalone (no prod HA story); per-instance independent mode (config matrix doubles, no real-world need). | §3.6 / §4.6 / T0.9 |
| **B31** | 2026-05-04 | Chat API | Rate-limit Redis was declared (B27) and probed by `/readyz` (T7.7) but had no consumer — dead infrastructure declaration. | **Per-user fixed-window rate limit on `/chat` and `/chat/stream`**: `CHAT_RATE_LIMIT_PER_MINUTE` (default 30) over `CHAT_RATE_LIMIT_WINDOW_SECONDS` (default 60). `RateLimiter` adapter (`clients/rate_limiter.py`, T3.14) uses `INCR` + `EXPIRE` on the rate-limit Redis instance with key `ratelimit:chat:{user_id}`. Composition root exports a FastAPI `Depends` factory; chat router declares `dependencies=[Depends(chat_rate_limit_dep)]` (T3.16) — router-level, **not** global middleware, so ingest / health / MCP are unaffected. Excess returns 429 `application/problem+json` with `error_code=CHAT_RATE_LIMITED` (§4.1.2) and a `Retry-After` header equal to seconds until window reset (S37). | (a) Drop the rate-limiter from P1 entirely (removes infrastructure declared in B27/B28; defers a defence against LLM-cost runaway to P2); (b) global middleware on every endpoint (ingest and health endpoints would compete for the same per-user budget — wrong scope); (c) sliding-window or token-bucket (more accurate but `INCR + EXPIRE` is one RTT; sliding window needs `ZADD + ZREMRANGEBYSCORE + ZCARD`, ~3× cost for marginal accuracy gain at this throughput). | §3.4 / §4.1.2 / §4.6 / §6.2 / B27 / T3.13 / T3.14 / T3.15 / T3.16 / T7.5a |
| **B30** | 2026-05-04 | Operator UX | What does an operator have to do to bring up the system end-to-end? | **Two-command quickstart**: `cp .env.example .env` → fill required vars → `python -m ragent.api` (T7.5d) and `python -m ragent.worker` (T7.5e). All else is automatic: schema/index auto-init runs from FastAPI lifespan + worker startup (T0.8d, idempotent); composition root (T7.5a) wires every dependency from env vars, no per-module env reads; TaskIQ broker module (T0.10) is the single import point for `@broker.task` decorators; `.env.example` (T0.11) is symmetric with spec §4.6 (drift test T0.11a). Project module layout fixed in §6.2 — every plan row produces exactly one file in that tree. Reconciler is K8s-only and not required for the local two-command path (recovery surface, not steady-state). E2E quickstart asserted by T7.2 launching the real entrypoint subprocesses, not internal scaffolding. | (a) Manual `alembic upgrade head` step before boot — adds an operator-facing migration command, defeats "two commands"; (b) per-module env reads — couples every module to env, blocks DI testing; (c) split broker module per task — multiple import paths, decorator misregistration risk; (d) no `.env.example` — operator reads spec §4.6 by hand, easy to miss required vars and discover at first failed request; (e) free-form module layout — names drift between plan and code, integration tests import wrong path. | §1 / §3.1 / §4.6 / §6.1 / §6.2 / T0.10 / T0.11 / T7.5 / T7.5a–f / T7.2 |
| **B29** | 2026-05-04 | Chat API | Optional retrieval filter by `source_app` / `source_workspace` | **Filter in ES via denormalised keyword fields.** `chunks_v1` mapping gains two `keyword` fields (`source_app`, `source_workspace`) populated by `VectorExtractor` from `documents` at ingest. Chat request schema (§3.4.1) accepts both as optional fields; when present they apply as ES `term` filter in **both** retrievers' `filter` clause (kNN `filter`, BM25 `bool.filter`). AND semantics when both supplied. Empty string ⇒ 422 `CHAT_FILTER_INVALID`. These are scope metadata, not auth fields (B14 distinction preserved); permission gating remains a separate post-retrieval layer (§3.5). | Post-retrieval filter via document JOIN in SourceHydrator (forces over-fetch with unbounded `K' = K × overfetch_factor` — narrow workspaces silently truncate); filter on `documents` only, retrieve all chunks then drop (defeats kNN top-K semantics); add a third retriever per filter combination (mapping bloat, no win). Pre-existing `chunks_v1` data does not exist (still pre-implementation), so single-version mapping update is safe; would otherwise require `chunks_v2` + reindex. | §3.4 / §3.4.1 / §4.3 / §4.4 / §5.2 / `resources/es/chunks_v1.json` / T1.9 / T1.12 / T3.5 |
| **B35** | 2026-05-07 | Schema | Rename `documents.source_workspace VARCHAR(64) NULL` to `source_meta VARCHAR(1024) NULL` (free-format). Supersedes the `source_workspace` naming and width chosen in B11/B29. | **Rename + widen.** Column on `documents`, denormalised keyword on `chunks_v1` (with `ignore_above: 1024`), Pydantic field on ingest/chat/retrieve schemas, all repository / service / worker / pipeline references. Validator caps stay tiered: `source_app` ≤ 64 (still a keyed namespace), `source_meta` ≤ 1024 (free-format). Migration `005_rename_source_workspace_to_source_meta.sql` does `ALTER TABLE … CHANGE COLUMN`; crossing the VARCHAR length-prefix boundary (≤255 vs >255) means MariaDB falls back to ALGORITHM=COPY (brief table lock on prod). ES mapping updated in `resources/es/chunks_v1.json`; existing clusters need a reindex on upgrade — fresh installs pick up the new mapping automatically via boot auto-init (B26). | (a) Keep `source_workspace` and stretch its semantics to "any string" — name lies about scope, every new caller has to read the spec to know it's free-format; (b) drop the field — caller-side metadata is a real need (slack channel, S3 prefix, generic tags) and B29 already wired it into retrieval filters; (c) add a parallel `source_meta` and keep `source_workspace` for compat — two near-identical columns, ambiguous which one drives the filter. | §3.1 / §3.4.1 / §3.4.4 / §4.1.2 / §4.3 / §4.4 / §5.1 / §5.2 / B11 / B29 / `migrations/005_*.sql` / `resources/es/chunks_v1.json` |
| **B32** | 2026-05-07 | Architecture | When to introduce the document/revision split (`documents` + `document_revisions` + `active_revision_id`). | **Defer to Phase 2.** Phase 1 closes the existing supersede bugs only (cascade through `self.delete`, DB-side survivor guard in `pop_oldest_loser_for_supersede`). The revision split is a multi-day track touching repository, service, worker, reconciler, ES mapping, retrieval pipeline, and API shape; it lands on its own branch with its own plan.md entries. Design captured in `docs/team/2026_05_07_revision_model_proposal.md` (motivation §1, schema §4, code surfaces §5). **2026-05-15 update (B50):** The embedding-model-migration motivation for this split is **withdrawn** — B50's multi-vector single-index design provides a safe, zero-downtime model swap without needing `document_revisions`. Any remaining motivation for the split (e.g. reingest mid-flight retrieval consistency beyond what B39/B41 already cover) must stand on its own; it is no longer a blocker for embedding-model evolution. | (a) Land in Phase 1 — too large for current branch, blocks unrelated work; (b) skip entirely — leaves "two READY rows produce mixed retrieval results during reingest" as a UX consideration (embedding-model-migration aspect now handled by B50); (c) build a smaller "active flag" instead of revisions — would not solve embedding-model coexistence at the time of this row, since B50 was not yet designed. | §3.1 / §3.4 / B50 / `docs/team/2026_05_07_revision_model_proposal.md` |
| **B36** | 2026-05-08 | Retrieval | `_SourceHydrator` semantics on hydration miss — should a chunk whose `document_id` has no matching READY row be dropped or passed through with empty source fields? | **Drop.** Hydrator becomes the correctness gate: orphan ES chunks (post-DELETE), mid-flight rows (PENDING/UPLOADED/FAILED), and demoted rows (DELETING) never reach LLM context or `sources[]`. Decouples retrieval correctness from cleanup completeness — `fan_out_delete` failures, reconciler outages, or revision demotion latency become disk-reclaim concerns, not user-visible bugs. Cost: retrieval result count may be lower than ES recall when stale chunks exist; this is the desired behaviour. | (a) Pass through with `source_title=null` (current pre-B36) — orphan chunk content reaches LLM verbatim, citations show "unknown", silently corrupts answers; (b) ES-side filter joining `documents` at query time — Haystack ES integration does not support cross-index joins, requires custom retriever; (c) defer to active_revision_id (P2 revision model) — ties P1 correctness to multi-day P2 track. | §3.4 / S6j / `pipelines/retrieve.py::_SourceHydrator` |
| **B37** | 2026-05-08 | Bootstrap | Should `composition.build_container()` still hard-require legacy `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` when `MINIO_SITES` JSON is set? | **No.** When `MINIO_SITES` is configured, the legacy single-site `MinIOClient` becomes redundant — `MinioSiteRegistry` covers every IO path. `/readyz` minio probe switches from `container.minio_client` to `container.minio_registry.default().client`. Operator following `.env.example` (which marks legacy three as DEPRECATED) can boot with only `MINIO_SITES` set. Legacy vars remain honoured when `MINIO_SITES` is absent — synthesised into a `__default__` entry by `MinioSiteRegistry.from_env()`. | (a) Keep current behaviour (both required) — contradicts `.env.example` DEPRECATED marker, every operator hits sys.exit on first boot; (b) drop legacy support entirely — breaks any caller still on single-site env; (c) make legacy the source of truth and synthesise `MINIO_SITES` from it — defeats v2 multi-site design. | §4.6.2 / B30 / T-RR.4 / T-RR.5 / T-RR.6 |
| **B38** | 2026-05-08 | Bootstrap | TokenManager J1→J2 exchange validation timing — first-request lazy or boot-time pre-warm? | **Boot-time pre-warm in `_check_infra_ready`.** Each `TokenManager` in `container.token_managers` runs `get_token()` during the lifespan startup probe; failure raises and aborts boot. A wrong `AI_API_AUTH_URL` or stale `AI_*_J1_TOKEN` surfaces before `/livez` returns 200, so a green readiness probe truly means the AI dependency chain is reachable. Current lazy behaviour is preserved beyond boot — refresh-margin logic still triggers on subsequent requests near expiry. | (a) Stay lazy (current) — `/livez` and `/readyz` both green while AI auth is broken; first chat or first ingest task 500s opaquely; (b) periodic background warm — adds a third long-lived task to manage; lazy already covers expiry; (c) probe at `/readyz` instead of `_check_infra_ready` — would refuse to serve traffic but boot still succeeds; conflates dependency outage (transient) with credentials misconfig (permanent). | §3.6 / `bootstrap/app.py::_check_infra_ready` / `clients/auth.py::TokenManager` / T-RR.7 / T-RR.8 |
| **B39** | 2026-05-08 | Ingest | When a worker finishes a re-ingest of an existing `(source_id, source_app)`, should the new doc's `READY` transition also atomically demote prior `READY` siblings, or stay deferred to reconciler-driven supersede? | **Atomic promote-and-demote in the same tx.** Worker's READY transition becomes `UPDATE … SET status='READY' WHERE document_id=:new AND status='PENDING'; UPDATE … SET status='DELETING' WHERE source_id=:src AND source_app=:app AND document_id != :new AND status='READY'`. Combined with B36, retrieval transitions to the new revision the moment the worker's tx commits — no race window where both old and new are READY and both retrievable. Reconciler still runs supersede, but only as belt-and-suspenders for the case where the worker dies between the two UPDATEs (the second is idempotent on resume). | (a) Status quo (reconciler tick supersede) — race window unbounded if reconciler stalls; users see new+old mixed in chat; (b) Phase 2 active_revision_id pointer — semantically cleaner but multi-day track and not required to close the race; (c) demote at promote time but in a separate tx — re-introduces a race window (smaller but non-zero). | §3.1 / §3.4 / B32 / T-RR.9 / T-RR.10 |
| **B40** | 2026-05-08 | Ingest | Should HTTP `DELETE /ingest/{id}` actually invoke `PluginRegistry.fan_out_delete`, or rely on B36 hydrator drop + reconciler reclaim? | **Yes — invoke synchronously.** Spec §3.1 step 1 already prescribes the cascade order; current implementation skips it because `IngestService._broker` (a `TaskiqDispatcher`) lacks `fan_out_delete`. Fix wires `container.registry` (`PluginRegistry`) into `IngestService` and removes the `_has_fan_out` introspection branch. ES chunks are purged in the request scope so disk reclaim does not depend on reconciler activity. Worst-case HTTP latency bounded by `PLUGIN_FAN_OUT_TIMEOUT_SECONDS` (default 60); ES `delete_by_query` is sub-second in practice. **B36 still required** — protects the failure path where fan_out partially completes and the row is gone before all chunks are. | (a) Keep skipping — relies entirely on reconciler + B36 to mask orphans; ES disk grows unbounded between reconciler ticks; (b) async dispatch via TaskIQ — replaces reconciler with broker as load-bearing retry surface (same reliability tier); (c) outbox table + sweeper — duplicates reconciler at table level. | §3.1 / B3 audit / T-RR.11 / T-RR.12 / T-RR.13 |
| **B41** | 2026-05-09 | Ingest | B39 closed the both-READY race for **in-order** worker completion, but if an older worker finishes after a newer revision was already created (or already promoted), naively demoting "any other READY sibling" lets the older revision incorrectly win until reconciler-driven supersede arbitrates. Should the worker promote be DB-arbitrated, or accept the residual window? | **DB-arbitrated promote.** `promote_to_ready_and_demote_siblings` does `SELECT document_id FROM documents WHERE source_id=:src AND source_app=:app AND status IN ('PENDING','READY') ORDER BY created_at DESC, document_id DESC LIMIT 1 FOR UPDATE` to elect the survivor. If caller is the survivor → promote + demote prior READY (B39 path). If not → self-demote PENDING → DELETING in the same tx; the worker also gates post-READY enrichment (`registry.fan_out`) on the returned `bool`. Result: retrieval correctness holds from the worker's tx alone for any worker completion order, and reconciler is **safety-net only** — never load-bearing for user-visible state. | (a) Status quo (B39 + reconciler arbitration) — leaves a window where retrieval flips to the older revision until reconciler tick; reconciler becomes load-bearing for correctness; (b) Reject promote when not survivor (raise) — worker would crash + retry forever on permanently-superseded docs; (c) `active_revision_id` pointer (Phase 2) — semantically cleaner but a multi-day track and not required to close this race. | §3.1 / B36 / B39 / T-RR.14 / T-RR.15 |
| **B42** | 2026-05-08 | Testing | Integration-test ES container has no `analysis-icu` plugin (vanilla `elasticsearch:9.2.3` from `testcontainers`); prod mapping (B26) uses `icu_text` analyzer that requires the plugin → naïvely loading the prod mapping into the test ES fails at index creation. | **Two mapping files, env-driven dir override.** Prod loads `resources/es/chunks_v1.json` (ICU). Tests load `tests/resources/es/chunks_v1.json` (default `standard` analyzer, structurally identical otherwise) by setting `RAGENT_ES_RESOURCES_DIR` in `tests/conftest.py`; `init_es()` reads this env and falls back to the prod path. Drift test (`test_es_resource_drift.py`) continues to pin `resources/es/chunks_v1.json` ↔ spec §5.2; a parallel test pins the test mapping file's structural equality (analyzer field + ICU `analysis` block being the only deltas). **Risk accepted:** CJK BM25 behaviour (S36) is **not** covered by integration tests under this setup; covered by manual / staging smoke against an ES with `analysis-icu` installed (`Dockerfile.es-test`). | (a) Build `Dockerfile.es-test` for every test run — ~30–60s docker build cost per CI cold cache, rejected as too heavy; (b) bake `standard` analyzer into the single prod mapping — defeats B26 (CJK retrieval breaks in prod); (c) parametrize analyzer name via env inside one mapping file (template substitution) — adds an env per substitution surface and obscures what prod actually ships with. | §5.2 / §7 / `resources/es/chunks_v1.json` / `tests/resources/es/chunks_v1.json` / `tests/conftest.py` / `src/ragent/bootstrap/init_schema.py` |
| **B28** | 2026-05-04 | Config | Env-var inventory was incomplete — missing datastore connections (MariaDB/ES/MinIO host/creds), J1 client credentials, HTTP bind, OTEL exporter, retry/timeout policy knobs, upload limits, and log level; `RERANK_API_URL` was misspelled `REREANK_API_URL` | **Reorganise §4.6 into 8 subsections** (bootstrap, datastore, redis, third-party clients, worker/reconciler, pipeline/chat, per-call timeouts, observability). **Add 26 new vars** covering every previously implicit literal: `MARIADB_DSN`, `ES_HOSTS`/`ES_USERNAME`/`ES_PASSWORD`/`ES_API_KEY`/`ES_VERIFY_CERTS`, `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_SECURE`, `RAGENT_HOST`/`RAGENT_PORT`/`LOG_LEVEL`, `AI_API_CLIENT_ID`/`AI_API_CLIENT_SECRET`, `WORKER_MAX_ATTEMPTS`, `RECONCILER_PENDING_STALE_SECONDS`/`RECONCILER_UPLOADED_STALE_SECONDS`/`RECONCILER_DELETING_STALE_SECONDS`, `INGEST_MAX_FILE_SIZE_BYTES`, `INGEST_LIST_MAX_LIMIT`, the seven per-call timeouts (`EMBEDDER_INGEST/QUERY`, `ES_BULK/QUERY`, `MINIO_GET/PUT`, `LLM`, `PLUGIN_FAN_OUT`, `READYZ_PROBE`), plus four `OTEL_*` vars. **Fix typo** `REREANK_API_URL` → `RERANK_API_URL`. **Rename** ambiguous `RECONCILER_STALE_AFTER_SECONDS` to per-state `RECONCILER_PENDING_STALE_SECONDS` and add UPLOADED/DELETING siblings. **Change `MINIO_BUCKET` default** from `ragent-staging` → `ragent` (B10/B25 prose updated). Also adds an inventory rule: any literal value read by code that is not represented in §4.6 is a spec drift bug. | Leave datastore connections as "implicit per-environment overrides" (every operator reinvents the wheel; bootstrap module has no canonical names to read); expose only DSN-style strings for ES/MinIO too (forces credential concatenation in URLs, harder to rotate); keep timeouts as code constants only (violates J21 rule "every call site lists per-call timeout AND aggregate ceiling"); ship without J1 creds (TokenManager has a URL but no way to authenticate — boot succeeds but every embedder/LLM call fails on first request). | §1 / §3.1 / §3.6 / §3.7 / §4.5 / §4.6 / §6.1 / T0.8 |
| **B47** | 2026-05-11 | API/MCP | P1 reserved `POST /mcp/v1/tools/rag` as a 501 stub with REST-shape `{query: str}`. P2.5 needs a real handler. Three options: (A) keep REST shape, (B) full MCP JSON-RPC 2.0 server, (C) REST core + thin MCP wrapper. | **Option B — real MCP JSON-RPC 2.0 server** at `POST /mcp/v1`, single endpoint dispatching by `method` field. Implements `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping` (§3.8.2). Sole tool `retrieve` wraps the existing `POST /retrieve/v1` pipeline (NOT chat — calling agent's LLM does the synthesis). Transport: streamable HTTP request/response subset (no SSE in P2.5). Protocol revision pinned to `2024-11-05`. Stateless; no `Mcp-Session-Id` session. JSON-RPC errors carry `data.error_code` matching the existing `HttpErrorCode` catalog so JSON-RPC and HTTP errors correlate. Auth (401) is transport-layer `application/problem+json`, NOT a JSON-RPC error envelope. The P1 `/mcp/v1/tools/rag` 501 endpoint is **removed**. | (A) Keep REST shape under `/mcp/` URL: misrepresents the protocol — stock MCP clients (Claude Desktop, Cursor) cannot register the server. (C) REST + MCP wrapper: two surfaces with identical behavior duplicate test matrix; YAGNI until both client types are confirmed. (D) Stateful MCP with `Mcp-Session-Id`: adds session storage requirement; not needed for a single read-only tool. (E) Wrap chat pipeline instead of retrieve: confuses MCP semantics — tools return data, the calling LLM reasons; chat already does both inside ragent. | §3.8 / §4.1 / §4.1.2 / §4.6.6 / S58–S67 / P2.5 |
| **B49** | 2026-05-11 | SRE/QA | The §3.6 resilience claims (reconciler ≤ 10 min recovery, idempotent partial-failure handling, fail-open reranker, mid-stream error framing) ship as prose in spec but have no executable evidence beyond C1 (single worker-kill case, currently `xfail(run=False)`). Per journal 2026-05-08 E2E gate integrity rule, every spec-declared SLO needs a test in some automated gate. | **Chaos drill suite under `tests/e2e/test_chaos/test_C<N>_<scenario>.py`** — six cases C1–C6 covering worker SIGKILL, MariaDB↔ES split-brain, ES bulk 207 partial, rerank 5xx, LLM stream interrupt, MinIO transient 503 (matrix in §3.6.1). Gated by a **nightly CI lane** (not per-PR — slow + inject delays), each case asserts the same four invariants (terminal status, ES/DB consistency, OTEL spans present, `chaos_drill_outcome_total{case,outcome}` increment). C1 unblocks the existing `test_chaos_worker_kill.py` (lift `xfail(run=False)`); C2–C6 are new files. Acceptance: nightly green for ≥ 3 consecutive runs before P2.6 軌三 marked done. | (a) Per-PR gate: ~5–10 min overhead per PR + WireMock state pollution between cases. (b) Manual quarterly drills: same failure mode as the silent `pytest.skip()` problem (journal 2026-05-08) — no automated signal. (c) Single chaos test parameterised over all cases: a shared fixture failure cascades all 6 to red; per-case files isolate diagnosis. | §3.6.1 / §3.7 / P2.6 軌三 / journal 2026-05-08 E2E gate integrity |
| **B50** | 2026-05-15 | Architecture / Ops | Future embedding-model swap needs to be a runbook-only operation (zero downtime, zero restart, painless rollback, rollback-window write safety). B33 deferred per-document routing to Phase 2 — but a swap design is needed now to remove the hardcoded `bge-m3` / 1024 in `clients/embedding.py` and `resources/es/chunks_v1.json`, and to lay forward-compatible foundations. | **Multi-vector single-index design with a five-API admin lifecycle.** ES `chunks_v1` carries multiple per-model vector fields side-by-side during migration (`embedding_<model_normalized>_<dim>`); a `system_settings` table (4 keyed rows: `embedding.stable`, `embedding.candidate`, `embedding.read`, `embedding.retired`) is the single source of truth, read by App via a TTL-cached `ActiveModelRegistry`. State machine `IDLE ⇄ CANDIDATE ⇄ CUTOVER` driven by five admin endpoints (`promote`, `cutover`, `rollback`, `commit`, `abort`) under `/embedding/v1`. Dual-write keeps every chunk's stable + candidate vector current throughout the migration so rollback is stateless even if doc updates land mid-window. Forward-compatible with B33 (the multi-vector field pattern becomes B33's per-doc routing key) and B34 (`embedding.retired` is a lightweight subset of revision-level retention). Full design in `docs/team/2026_05_15_embedding_model_lifecycle.md`. | (a) Alias-flip between two physical indexes — requires worker restart at env flip, and new docs ingested between alias flip and rollback land only in the new index, defeating the "rollback within window" claim; (b) build full B33 per-document routing today — multi-day track, blocks current branch on Phase-2 scope; (c) leave `bge-m3` hardcoded and document a Reindex+config-edit runbook — every future swap becomes a code change, not a runbook. | §3.2 / §3.4 / B33 / B34 / B35 / `docs/team/2026_05_15_embedding_model_lifecycle.md` |
| **B54** | 2026-05-16 | Retrieval | How to introduce user feedback (like/dislike) as a ranking signal without building a model-training pipeline or bloating MariaDB with chat content? **(renumbered from B50; collision with embedding-lifecycle B50 detected at PR #80 merge)** | **Non-parametric feedback memory retriever.** A new `_FeedbackMemoryRetriever` Haystack component runs as a **3rd retriever** alongside vector + BM25, doing kNN over a new ES `feedback_v1` index of `(query_embedding, source_app, source_id, vote, reason, ts)` records. Results join via the existing `DocumentJoiner` with **weighted RRF** (`weights=[1.0, 1.0, CHAT_FEEDBACK_RRF_WEIGHT]`, default `0.5`). Feedback is **source-level only** (not chunk-level) — keyed on the `(source_app, source_id)` pair per B11/B35; aligns with B36 hydrator's `document_id` correctness gate and survives chunk re-ingest. Per-source score = `wilson_lower_bound(likes, likes+dislikes, z=1.96) × 0.5^((now-ts_max)/CHAT_FEEDBACK_HALF_LIFE_DAYS)`, gated by `(likes+dislikes) ≥ CHAT_FEEDBACK_MIN_VOTES`. Behaves as instance-based supervised learning (memory-based collaborative filtering); no training infrastructure. **Latency budget:** kNN call bounded by `ES_QUERY_TIMEOUT_SECONDS` (§4.6.7, default 10s) like the existing two retrievers. **Concurrency:** P1 runs the three retrievers sequentially (matching the current §3.4 "P1 OPEN" note — retrievers sequential, P2 AsyncPipeline parallelises); worst-case latency = sum of three ES query budgets. **Chunk lookup after Wilson scoring:** one MariaDB `(source_app, source_id) → document_id` lookup then a single ES `terms` query on `chunks_v1.document_id` (no N+1). Only active when `CHAT_JOIN_MODE=rrf` AND `CHAT_FEEDBACK_ENABLED=true`. | (a) Fine-tune embedder on feedback hard-negatives — strictly higher ROI long-term but needs training infra, A/B harness, and data accumulation; multi-week effort, defers MVP indefinitely. (b) Native ES RRF retriever with feedback as a sub-query — locks fusion into ES query DSL, breaks `CHAT_JOIN_MODE` topology dispatch (C6) and blocks future graph / reranker branch insertion at component level. (c) Per-doc `like_count` denormalised onto `chunks_v1` — query-independent popularity heuristic; cannot learn "this source is good FOR THIS QUERY TYPE"; no defence against cold-start or 1-vote noise. (d) Pairwise cross-encoder reranker training from feedback — same training-infra blocker as (a); revisit in P3. | §3.4 / §3.4.5 / §4.6.6 / §5.1 / §5.4 / T-FB.1–T-FB.12 |
| **B55** | 2026-05-16 | Retrieval | Where to persist the `(query, shown_sources)` snapshot that feedback semantically references — eager-write a `chat_traces` row on every `/chat` call (99% dead weight given <1% feedback rate), or lazy-write only when feedback arrives? **(renumbered from B51)** | **Lazy write — no DB write on `/chat` path.** `/chat` response carries `request_id` (UUIDv7) + `feedback_token = HMAC(FEEDBACK_HMAC_SECRET, canonical_json({request_id, user_id, sources_hash, ts}))` where `sources_hash = sha256(json([[source_app, source_id], …]))` over the (source_app, source_id) **pair** list (document identity per B11/B35). `POST /feedback/v1` body echoes `{request_id, feedback_token, query_text, shown_sources: list[{source_app, source_id}], source_app, source_id, vote, reason?}`. Server verifies HMAC, asserts `body.request_id == payload["request_id"]` and (when `X-User-Id` is present) `X-User-Id == payload["user_id"]` to defeat token replay / cross-user reuse (PR #80 codex review), checks the voted pair ∈ shown, re-embeds `query_text` once per feedback event, dual-writes **MariaDB `feedback` first, then ES `feedback_v1`** (matches `documents`→`chunks_v1` ordering, B36 invariant: MariaDB is SoT). MariaDB unique key is `(user_id, request_id, source_app, source_id)`; ES `_id = sha256(user_id|request_id|source_app|source_id)`. ES write failure logs `event=feedback.es_write_failed` and increments `ragent_feedback_es_write_failed_total`; MariaDB row remains the truth and an offline replay job (P2) can re-derive `feedback_v1`. Token TTL = 7 days; past 7d → 410 `FEEDBACK_TOKEN_EXPIRED`. No `chat_traces` table needed — HMAC fully integrity-guards the client-carried snapshot. | (a) Eager-write `chat_traces` on every `/chat` — 99% rows dead weight at <1% feedback rate; MariaDB bloat without observable benefit; conflicts with project policy "text/content goes to ES, MariaDB stores meta only" (§5.1). (b) Ephemeral snapshot in Redis with TTL=24h, promoted on feedback arrival — adds Redis as load-bearing for correctness; cache eviction = silent data loss with no audit trail. (c) Trust client snapshot without HMAC — single authenticated user could poison `feedback_v1` by claiming arbitrary `shown_sources`; HMAC adds one ms of CPU and closes the gap. (d) Token without expiry — replay-attack window unbounded; 7d matches the analytical-value cutoff. (e) Two-phase commit / transactional outbox MariaDB↔ES — over-engineered for a write where ES failure is recoverable from MariaDB. (f) Bind only `source_id` (not the pair) in sources_hash — a client could forge the `source_app` for a known `source_id` (PR #80 gemini security-high). | §3.4 / §3.4.5 / §4.6.6 / §5.1 / `routers/feedback.py` / `routers/chat.py` / `utility/feedback_token.py` |
| **B56** | 2026-05-16 | Retrieval | `POST /feedback/v1` accepts a `reason?` field; free-text comment or closed enum? Which values? **(renumbered from B52)** | **Closed enum, 6 values, frozen Day 1:** `irrelevant \| hallucinated \| outdated \| incomplete \| wrong_citation \| other`. Maps 1:1 to the four RAG failure layers (retrieval / grounding / index-freshness / coverage) plus citation and a catch-all. **New enum values require a new B-row** (append-only, never re-edit). No free-text `comment` field in MVP — `other` is the escape hatch; PII-scrubbing surface deferred until comment is actually shipped. Each reason routes to a different downstream behaviour: `irrelevant` → query-conditional down-rank (P2); `outdated` → re-ingest trigger (manual P1, automated P3); `hallucinated` / `wrong_citation` → generation-layer signals, retrieval unchanged; `incomplete` → query-expansion hint (P3); `other` → analytics only. | (a) Free-text `reason` — defeats aggregation; cannot dashboard or filter; PII-scrubbing burden from day 1. (b) Loose enum without B-row freeze — taxonomy drift breaks historical analytics joins; one enum-value rename invalidates months of training data. (c) Binary "this is bad" tag, no reason — collapses all failure modes into one signal; same low-resolution problem as no-reason dislike (forces all remediation to be "blanket model retrain"). (d) Defer reason collection to P2 — collecting structured reason from day 1 is cheap; retrofitting taxonomies onto historical raw votes is impossible. | §3.4.5 / §4.1.2 / `schemas/feedback.py` / `_FeedbackMemoryRetriever` (reason-conditional filter, P2) |
| **B57** | 2026-05-16 | Retrieval | Which feedback-system capabilities ship in P1 MVP vs deferred? Risk of over-engineering before observing real feedback distribution. **(renumbered from B53)** | **P1 MVP ships only the closed-loop minimum:** HMAC token, `POST /feedback/v1`, dual-write, kNN retriever with Wilson + time-decay + min-votes gate, weighted RRF fusion at `CHAT_FEEDBACK_RRF_WEIGHT=0.5`, default `CHAT_FEEDBACK_ENABLED=false` (ship dark). **Deferred to P2+:** (1) Inverse Propensity Score (IPS) reweighting for position-bias debiasing — `position_shown` is recorded into MariaDB `feedback` from day 1 (zero cost) but not consumed; (2) Exploration / ε-greedy reservation of top-K slots for non-boosted candidates; (3) Reason-driven filter blacklisting (e.g. `irrelevant`-clustered sources auto-quarantined); (4) Automated `outdated` → re-ingest trigger; (5) Fine-tune embedder / reranker from accumulated `feedback` rows; (6) **Retention** — `feedback` table and `feedback_v1` index are append-only with no TTL in P1; query-side `ts > now - 90d` filter bounds retrieval-time impact but storage grows linearly with feedback events (estimated <1% chat rate ⇒ tolerable for P1 volumes); a reconciler-driven sweep keyed on retention window deferred until first P2 ops review; (7) MariaDB↔ES parity reconciler for `feedback` ↔ `feedback_v1` (analogue of B36 for chunks) — MVP relies on offline replay from MariaDB SoT to backfill ES on rare write failures. Re-evaluate each deferral after ≥ 3 months of dark-mode write-only data, or when online enable shows positive A/B lift plateau. | (a) Ship IPS from day 1 — adds click-model estimation + position-aware weighting; risk of premature complexity before observing actual position-bias magnitude; can be applied offline later from raw `feedback` table without backfill. (b) Skip Wilson, use raw `likes − α·dislikes` — small-sample noise dominates (single user voting twice swings ranking); Wilson is a one-line utility with strictly better behaviour. (c) Default `CHAT_FEEDBACK_ENABLED=true` — risks unexpected retrieval changes for current users; ship dark, observe write volume + dashboard reason distribution first. (d) Cut reason collection from MVP — schema cost is one nullable column; collecting from day 1 enables all reason-driven P2 features without backfill. | §3.4.5 / §4.6.6 / `_FeedbackMemoryRetriever` / T-FB.1–T-FB.12 / future plan rows |
| **B58** | 2026-05-19 | ES | `chunks_v1.embedding.index_options.type` — reversal of B26's P1 `flat` choice. With a 1024-dim corpus, the recall delta between `flat` (exact brute-force kNN) and `bbq_hnsw` (Better Binary Quantization HNSW, ES 8.16+) sits well inside chat retrieval's RRF tolerance, while `bbq_hnsw` saves ~32× heap memory. Original B26 deferred to P2 "once `flat` query latency stops meeting the chat budget"; deferring further now adds a Phase-2 reindex step for no observable P1 benefit. | **Flip to `bbq_hnsw` in P1, fresh-install only — no reindex window.** Resource file `resources/es/chunks_v1.json` and the spec §5.2 JSON mirror both move to `bbq_hnsw`; bootstrap auto-init keeps its "PUT if absent" semantic, so any existing dev/CI cluster that already has a `flat`-mapped `chunks_v1` MUST be wiped and recreated by the operator (no in-place mapping migration is supported by ES for `dense_vector.index_options`). `tests/resources/es/chunks_v1.json` keeps `flat` so vanilla `elasticsearch:9.2.3` CI containers stay light-weight; the structural-match invariant (`test_init_schema.py::test_test_mapping_structurally_matches_prod_except_documented_deltas`) tolerates the `index_options.type` delta alongside the B42 ICU delta. Existing prod clusters with data on `flat` are out of scope for this row — if they appear, a reindex runbook (`chunks_v2` + alias swap) becomes a separate B-row. **Cancels B26's fallback requirement:** the project hard-requires ES 9.2.3 (CLAUDE.md tech stack; `/readyz` verifies cluster version via `analysis-icu` probe), and 9.2.3 ≫ 8.16, so `bbq_hnsw` is always supported. The B26-era `event=es.bbq_unsupported` log + standard-HNSW fallback was a safety net for an open-ES-version posture that no longer applies; not implementing it is intentional. `ES_BBQ_UNSUPPORTED` error code (§4.1.2) remains as a forward-compat reservation only. | (a) Keep `flat` until P2 (status quo) — defers a benign config flip behind a corpus-size trigger that may never fire; no observable downside to flipping now on fresh installs. (b) Add a code-flag / env-var to pick `flat` vs `bbq_hnsw` per environment — defeats the "resource JSON is single source of truth" invariant (B26 §c) and adds an env-var with no operator decision input (every operator would pick the recommended value). (c) Reindex existing data to `bbq_hnsw` in this row — coupling a small spec flip to a multi-step runbook; split into a follow-up B-row only if any prod cluster turns out to be on `flat`. (d) Implement B26's fallback to standard HNSW for `bbq_hnsw_unsupported` cluster rejection — dead-code for ES 9.2.3+ guarantee; would add untested-in-CI exception branches whose only purpose is a scenario `/readyz` already rejects. | §5.2 / B26 / `resources/es/chunks_v1.json` / `tests/resources/es/chunks_v1.json` / `tests/unit/test_init_schema.py` |
| **B59** | 2026-05-19 | ES | Where to put the chunk row's "when was this last written to ES" timestamp — in a Python writer's `_source` dict (`DocumentEmbedder`, `VectorExtractor`, the B50 dual-write path) or via an ES ingest pipeline on the data plane? See `docs/00_journal.md` 2026-05-19 Architecture row for the full trade-off. | **ES ingest pipeline `chunks_default`** referenced from `settings.index.default_pipeline`. Source of truth: `resources/es/pipelines/chunks_default.json` (single `set` processor on `_ingest.timestamp → indexed_at`). New `indexed_at: date` mapping added to both prod and test `chunks_v1.json`. `init_schema.init_es` PUT-s every pipeline file BEFORE PUT-ing any index, because ES rejects index creation whose `default_pipeline` references a missing pipeline — pinned by `tests/unit/test_init_schema.py::test_init_es_puts_pipelines_before_indexes`. **Semantics — `indexed_at` = last write to ES, not first.** Under `DuplicatePolicy.OVERWRITE`, every retry / supersede reruns the pipeline and advances `_ingest.timestamp`; the `set` processor's `override: false` flag does NOT preserve first-write because it inspects only the incoming `_source`, never the stored doc. Operators who need true creation time read `documents.created_at` (MariaDB is SoT for that). **Upgrade caveat (PR #83 / Codex P1):** `init_es` is "PUT-if-absent"; an existing `chunks_v1` index keeps its old settings, so the new `default_pipeline` setting and `indexed_at` mapping field are **NOT** auto-applied to upgrades. Operator must either (i) wipe and recreate (`DELETE /chunks_v1` then restart App — accepts data loss; matches B58's wipe requirement) OR (ii) apply settings + reindex manually (`PUT /chunks_v1/_settings -d '{"index":{"default_pipeline":"chunks_default"}}'` + `_update_by_query` to backfill `indexed_at` on existing rows — pipeline only runs on NEW writes). The "no auto-ALTER existing indexes" stance is the project's day-1 init_schema contract (`init_schema.py` module docstring); generalised drift-detect + auto-migrate is a separate Phase-2 track. | (a) Stamp the field in every Python writer (`DocumentEmbedder`, `VectorExtractor`, B50 candidate-write path) — spreads time-source surface across N writers, opens application-clock skew between worker pods, requires every retry / supersede / cutover path to remember the convention, makes the timestamp invisible in `GET _mapping`. (b) Use a `script` processor that reads the existing doc via `ctx._source` to preserve first-write — slow, race-prone, breaks bulk throughput; only works at all if the write is a partial update, which ours isn't. (c) Use ES auto-generated `_seq_no` / `_primary_term` as a write monotonic — they survive overwrite but are not a wall-clock time and surface poorly to operators. (d) Name the field `created_at` while documenting last-write semantics — collides with the project-wide `created_at` convention (`documents.created_at`, `feedback.created_at`) which IS first-write; rejected to avoid documentation debt. (e) Auto-ALTER existing indexes in `init_es` to apply `default_pipeline` — violates the day-1 "PUT-if-absent" init contract; opens questions for other settings/mapping changes; better answered by a dedicated Phase-2 drift-detect framework. | §5.2 / §4.6.2 / `resources/es/pipelines/chunks_default.json` / `resources/es/chunks_v1.json` / `src/ragent/bootstrap/init_schema.py` / `tests/unit/test_init_schema.py` / `tests/integration/test_es_resource_drift.py` / `docs/00_journal.md` 2026-05-19 |
| **B60** | 2026-05-19 | Bootstrap | `ES_CHUNKS_INDEX` env audit (T-EI.1) threaded the override through every App-side consumer (`ElasticsearchDocumentStore`, `_FeedbackMemoryRetriever`, `VectorExtractor`, `Reconciler`, `/readyz` probe), but the bootstrap side (`init_es`) still used the resource **filename stem** as the index name. Result: when an operator sets `ES_CHUNKS_INDEX=foo`, bootstrap creates `chunks_v1` while App reads/writes `foo`. App-side gets dynamic-mapping `foo` or `/readyz` fails — exactly the mismatch T-EI.1 was meant to close. PR #83 gemini-code-assist high. | **`init_es` reads `ES_CHUNKS_INDEX` and uses it ONLY when PUT-ing the `chunks_v1.json` resource** (filename stem `chunks_v1` is the trigger for the env lookup). Other resources (e.g. `feedback_v1.json`) keep filename-as-name semantics — they have no env override. Pipeline files in `resources/es/pipelines/` always use their stem as pipeline id (no env override on pipeline id). One-line conditional inside `init_es`'s loop; pinned by `tests/unit/test_init_schema.py::test_init_es_uses_env_chunks_index_name_for_chunks_resource` and `::test_init_es_keeps_filename_stem_for_non_chunks_resources`. | (a) Generalised resource→env-var map — over-engineered for one current case; can grow when a second resource gains an env override. (b) Require resource files to declare their target index name via a top-level key (`"index_name": "{env:ES_CHUNKS_INDEX}"`) — adds a template syntax and a new schema for resource JSON. (c) Rename the resource file when env is overridden (e.g. operator copies `chunks_v1.json` → `foo.json`) — terrible UX, breaks drift test. (d) Give up the env-var, hardcode `chunks_v1` everywhere — reverts T-EI.1, but no real-world use of the override has been observed (latent feature for parallel-deploy scenarios). | §4.6.2 / T-EI.1 / T-EI.6 / `src/ragent/bootstrap/init_schema.py` / `tests/unit/test_init_schema.py` |
