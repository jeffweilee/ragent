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

**State machine:** `UPLOADED → PENDING → READY | FAILED`; `DELETING` transient on delete.

**API (v2, JSON only):** `POST /ingest/v1` discriminates on `ingest_type`:
- `inline` — UTF-8 `content` in body; staged to MinIO `__default__`. Ceiling: `INGEST_INLINE_MAX_BYTES` (10 MB).
- `file` — `{minio_site, object_key}`; HEAD-probed; worker reads caller's bucket. No copy. Ceiling: `INGEST_FILE_MAX_BYTES` (50 MB).
- `upload` — `multipart/form-data`; staged to MinIO `__default__`.

**MIME allow-list:** `text/plain`, `text/markdown`, `text/html`, DOCX, PPTX, `application/pdf`. Else → 415 `INGEST_MIME_UNSUPPORTED`.

**Source fields:** `source_id` + `source_app` (logical identity, mandatory) · `source_title VARCHAR(256) NOT NULL` · `source_meta VARCHAR(1024) NULL` (free-format, B35) · `source_url VARCHAR(2048) NULL` (display-only).

**Cleanup by `ingest_type`:**
| `ingest_type` | Post-READY auto-delete | `DELETE /ingest/{id}` reclaims |
|---|---|---|
| `inline`  | yes | yes (if pre-READY; blob already gone at READY) |
| `file`    | no (caller-owned) | no |
| `upload`  | no | yes (any status) |

**Locking:** atomic conditional `UPDATE … WHERE status IN (:accept_set)`; `rowcount=1` = won; `rowcount=0` = lost, no-op. No `SELECT FOR UPDATE` on single-row transitions. Pipeline body runs **outside any DB tx** — no row locks held during external calls (B16). Heartbeat: `updated_at=NOW()` every 30 s; Reconciler scans `updated_at < NOW() − 5 min`.

**Supersede (B41):** DB-arbitrated on READY transition — `SELECT … ORDER BY created_at DESC LIMIT 1 FOR UPDATE` elects survivor; non-survivor self-demotes PENDING→DELETING in same tx. At most one READY row per `(source_id, source_app)` at all times. No PUT/PATCH; mutation = re-POST with same pair.

**Create flow:**
1. Validation → MinIO stage → `documents(UPLOADED)` → kiq `ingest.pipeline` → `202 { task_id }`.
2. Worker TX-A (atomic claim) → heartbeat starts → pipeline body (idempotency clean → §3.2 → `fan_out`) → TX-B (READY or FAILED; FAILED also runs `fan_out_delete` + cleanup first).
3. Post-commit: MinIO delete (best-effort); on READY kiq `ingest.supersede`.

**Delete flow:** atomic claim → DELETING → outside-tx cascade: `fan_out_delete` → `delete_by_document_id` → optional MinIO delete → delete row → `204`. Mid-cascade failure: Reconciler resumes idempotently.

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
> → DocumentEmbedder (bge-m3 batched, bulk-writes to physical index / indices via ES bulk API)
> ```
> Each splitter sets `meta["raw_content"]` = exact byte slice (byte-stable, R4/S25). `_BudgetChunker` is the sole budget enforcer. `chunks_v1` stores both `content` (normalized, BM25-analyzed) and `raw_content` (`index: false`); LLM context and citations use `raw_content`.

**Performance & timeout discipline:**
- The pipeline's first step is `ChunkRepository.delete_by_document_id` + `PluginRegistry.fan_out_delete` (idempotency for retry — see §3.1; sweeps every plugin, not just `VectorExtractor`).
- `EmbeddingClient` is invoked in **batches of 32 chunks** per HTTP call (configurable; never 1-by-1).
- Every external call carries an explicit timeout: Embedder 30 s/batch (ingest), ES bulk 60 s, MinIO get 30 s, plugin `extract()` 60 s overall (enforced by `PluginRegistry.fan_out`).
- **Overall pipeline ceiling:** `INGEST_PIPELINE_TIMEOUT_SECONDS` (default 300 s, B18). Overrun ⇒ `FAILED` with `error_code=PIPELINE_TIMEOUT_AGGREGATE`.
- The pipeline body runs with no DB transaction open (see §3.1 locking discipline).

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

**Plugin construction (B17):** the Protocol freezes the **interface** (`extract`, `delete`, `health` plus three attributes) but plugins are **dependency-injected** via their constructor. `VectorExtractor.__init__(repo: DocumentRepository, chunks: ChunkRepository, embedder: EmbeddingClient, es: ElasticsearchClient)` — `extract(document_id)` reads `source_title` from `repo` and chunk rows from `chunks`. Plugins MUST NOT import `pipelines/` or HTTP layers; they accept their dependencies as constructor args, the registry simply holds the constructed instances.

**Registry:**
- `register()` raises `DuplicatePluginError` on name conflict.
- `fan_out(document_id)` → dispatch extract to all plugins concurrently; **per-plugin timeout 60 s** (overrun → `Result(error="timeout")`); `all_required_ok(results)` gates `READY`.
- `fan_out_delete(document_id)` → dispatch delete to all plugins concurrently; **per-plugin timeout 60 s**; runs **outside any DB transaction** (no row locks held during plugin network calls — P-E).

**BDD:**
- **S4 Protocol conformance** — Given an object missing any of `name` / `required` / `queue` / `extract` / `delete` / `health`, When `isinstance(obj, ExtractorPlugin)` is evaluated, Then it returns `False` (and `register()` raises before fan_out).
- **S5 stub no-op extract → READY** — Given a registered `StubGraphExtractor` (optional, no-op `extract`), When the worker runs `fan_out(document_id)`, Then `Result(ok=True)` is returned in 0 ms and `all_required_ok` does not depend on it (since `required=False`).
- **S11 duplicate registration** — Given a `PluginRegistry` already holding a plugin named `vector`, When `register()` is called with another plugin of the same `name`, Then it raises `DuplicatePluginError` and the existing instance is unaffected.

---

### 3.4 Chat Pipeline

```
QueryEmbedder → { ESVectorRetriever (kNN on embedding, optional filter)
                  ∥ ESBM25Retriever (multi_match text+title^2, optional filter)
                  ∥ FeedbackMemoryRetriever (kNN on feedback_v1; only when CHAT_FEEDBACK_ENABLED) }
              → DocumentJoiner (RRF, weights [1,1,CHAT_FEEDBACK_RRF_WEIGHT])
              → SourceHydrator (JOIN documents WHERE status='READY'; drops orphan chunks — B36)
              → LLMClient.{chat|stream}
```

- **Join mode (`CHAT_JOIN_MODE`):** `rrf` (default) | `concatenate` | `vector_only` | `bm25_only`.
- **Title (B15):** baked into every chunk's `embedding` at ingest (`embed(title + text)`); BM25 `title^2`. No extra retriever.
- **Filter (B29→B35):** optional `source_app` (≤ 64) / `source_meta` (≤ 1024) → ES `term` filter on both retrievers; AND when both. Empty string → 422 `CHAT_FILTER_INVALID`.
- **Two endpoints (B12):** `POST /chat/v1` (sync JSON) + `POST /chat/v1/stream` (SSE). Same §3.4.1 request schema.

#### 3.4.1 Request schema

```json
{ "messages":[{"role":"user","content":"…"}], "provider":"openai", "model":"gptoss-120b",
  "temperature":0.7, "max_tokens":4096,
  "source_app":"confluence", "source_meta":"eng", "top_k":20, "min_score":null, "dedupe":false }
```

`messages` required; `provider` validated against `{"openai"}` (B22); `top_k` 1–200; server prepends default system message when `role:"system"` absent.

#### 3.4.2 Response schema

```json
{ "content":"…", "usage":{"promptTokens":0,"completionTokens":0,"totalTokens":0},
  "model":"gptoss-120b", "provider":"openai",
  "sources":[{"document_id":"…","source_app":"…","source_id":"…","source_meta":"…",
               "type":"knowledge","source_title":"…","source_url":"…",
               "mime_type":"…","excerpt":"…","score":0.87}],
  "request_id":"…", "feedback_token":"<base64url>.<hmac>" }
```

`sources` null when empty; `excerpt` truncated to `EXCERPT_MAX_CHARS` (512) in router, LLM gets full text (B23). `feedback_token`+`request_id` only when `CHAT_FEEDBACK_ENABLED=true` AND `X-User-Id` present.

#### 3.4.3 Streaming (`/chat/v1/stream`)

```
data: {"type":"delta","content":"<token>"}

  … data: {"type":"done","content":"<full>","sources":[…]}


```

Mid-stream error (B6): `data: {"type":"error","error_code":"…","message":"…"}` then close. Pre-stream: normal RFC 9457.

#### 3.4.4 `POST /retrieve/v1`

Same pipeline as chat through `SourceHydrator`; no LLM. Request: `{query, source_app?, source_meta?, top_k?, min_score?, dedupe?}`. Response: `{"chunks":[…]}` (empty array, never `null`). `dedupe=true` keeps highest-scored chunk per `document_id`.

#### 3.4.5 `POST /feedback/v1` (B54/B55/B56)

Request: `{request_id, feedback_token, query_text, shown_sources, source_app, source_id, vote, reason?, position_shown?}`.
- Token TTL 7 days; HMAC binds `(request_id, user_id, sources_hash)` where `sources_hash = sha256(json([[source_app, source_id], …]))`.
- `vote ∈ {+1,−1}`; `reason` ∈ frozen enum (B56): `irrelevant|hallucinated|outdated|incomplete|wrong_citation|other`.
- Dual-write: MariaDB `feedback` first (SoT), then ES `feedback_v1`. ES failure → `204` + `ragent_feedback_es_write_failed_total++`.
- Errors: `401 FEEDBACK_TOKEN_INVALID` · `410 FEEDBACK_TOKEN_EXPIRED` · `422 FEEDBACK_SOURCE_INVALID`/`FEEDBACK_VALIDATION`.
- `_FeedbackMemoryRetriever` (3rd retriever, kNN on `feedback_v1`, Wilson+decay+min-votes) active only when `CHAT_FEEDBACK_ENABLED=true`.

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
- **JWT:** standard OIDC token. Carried in the `<RAGENT_JWT_HEADER>` request header as a **raw token, no `Bearer ` prefix** — clients send a raw JWT. **joserfc** (`joserfc` package — the actively-maintained successor to `authlib.jose`) verifies: signature against the JWKS published at `{scheme}://<OIDC_DOMAIN>/.well-known/jwks.json` (scheme = `https` unless `OIDC_USE_HTTPS=false`), `iss == OIDC discovery's "issuer"` (compared with trailing slashes stripped to absorb pydantic/IdP variance), `aud == <OIDC_AUDIENCE>`, `exp` ≥ now, `nbf` ≤ now. OIDC discovery + JWKS are fetched at composition (`build_container()`) via an injected `httpx.Client` (the same client controls `verify=...` for TLS verification — see `OIDC_VERIFY_SSL`), so a misconfigured `OIDC_DOMAIN` aborts startup rather than 500-ing the first protected request; the fetched JWKS is then cached in-process for the verifier's lifetime. Cache reuse is a contract requirement pinned by T8.1a tests. Once verified, `<RAGENT_JWT_CLAIM_USER_ID>` (default `preferred_username`) is extracted as the downstream `user_id`. Failure mapping: absent header / malformed token / bad signature / wrong `iss` / wrong `aud` / non-numeric `exp` / `nbf` in future / unknown `kid` / unsupported `alg` / any other verification error → 401 `AUTH_TOKEN_INVALID`; expired → 401 `AUTH_TOKEN_EXPIRED`; missing required claim → 401 `AUTH_CLAIM_MISSING`. `RAGENT_TRUST_X_USER_ID_HEADER=true` (non-prod only) bypasses JWT verification and reads `<RAGENT_USER_ID_HEADER>` directly.
- **Public paths (auth short-circuit):** the JWT middleware does not read the header or call the verifier for `/livez`, `/readyz`, `/startupz`, `/metrics`, `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json`. The set is a strict superset of `middleware/logging.py::_SKIP_PATHS` (which covers only the four operational health/metrics paths) — extended here with the FastAPI auto-docs surface. These endpoints are declared `P1 Auth = none` in §4.1 and remain auth-free in P2. The outbound MCP HUB (`src/ragent/mcp_hub/server.py`) runs as a separate process and is not covered by this middleware.
- **Header injection:** the extracted `user_id` is written into `<RAGENT_USER_ID_HEADER>` on the request scope so downstream routers (whose `Header(alias=...)` is bound to the canonical name) see the same value irrespective of the inbound auth mode.
- **Swagger/OpenAPI doc derivation (T8.D1):** `src/ragent/bootstrap/openapi.py::install_openapi` is called from `create_app` with the **same** env-resolved values that wire `_x_user_id_middleware`. The active mode publishes exactly one `apiKey` security scheme in `/openapi.json::components.securitySchemes` — `UserIdHeader` (name = `<RAGENT_USER_ID_HEADER>`) in trust-header mode, `JWT` (name = `<RAGENT_JWT_HEADER>`) in JWT mode — and tags every non-public operation with `security: [{<scheme>: []}]`. Public paths (§3.5 list) carry NO `security`. Per-route `Header(alias=...)` declarations are NOT the source of truth for docs; this generator is. Flipping `RAGENT_AUTH_DISABLED` / `RAGENT_TRUST_X_USER_ID_HEADER` / `RAGENT_JWT_HEADER` updates the middleware AND the Swagger Authorize button together — the two cannot drift.
- **Handler-side user_id source (T8.D2, T8.D3):** route handlers obtain the resolved `user_id` via `Depends(ragent.auth.deps.get_user_id)` — NOT via `Header(alias="X-User-Id")`. The dep reads `request.scope[SCOPE_USER_ID_KEY]` (populated by the middleware in both trust-header and JWT modes) with a header fallback for unit tests that bypass the middleware. Because `Depends(...)` is invisible to OpenAPI, the T8.D1 security scheme remains the single documented source. `tests/unit/test_no_auth_header_in_routes.py` is the anti-drift CI gate that fails collection if any router re-introduces `Header(alias="X-User-Id"|"X-Auth-Token")`.
- **PermissionClient (OpenFGA):** `batch_check(user_id, document_ids) → set[str]` post-filters retrieved chunks. Gated per-surface: `RAGENT_PERMISSION_INGEST_ENABLED` / `RAGENT_PERMISSION_CHAT_ENABLED` (both default `false`). Chat pipeline: ES retrieval → `batch_check` → `SourceHydrator → LLM`. May over-fetch K' = K × factor so K results remain after filtering.
- OpenFGA is fully encapsulated behind `PermissionClient`; never reaches the retrieval/ES path.

**BDD:**
- **S9 token refresh at boundary** — Given `TokenManager` cache holds a J2 with `expiresAt = T0 + 60 min`, When the wall clock advances to `T0 + 55 min` (`expiresAt − 5 min`) and a caller asks for the J2 token, Then `TokenManager` issues exactly one J1→J2 refresh HTTP exchange and returns the new token; 100 concurrent callers around the boundary share that single refresh (single-flight, P-F).
- Permission-gating BDD specified when the P2 plan is written.

---

### 3.6 Resilience

**Reconciler (Kubernetes `CronJob`, schedule `*/5 * * * *`, `SELECT … FOR UPDATE SKIP LOCKED`) — B9:**

> Implementation = a one-shot Python entrypoint (`python -m ragent.reconciler`) packaged in the same image, scheduled by **K8s CronJob** with `concurrencyPolicy: Forbid` and `successfulJobsHistoryLimit: 3`. Not a TaskIQ scheduled task (decouples sweeper liveness from broker health — Reconciler is the recovery surface for broker outage itself, see R1).

- `UPLOADED, updated_at < NOW() - 5 min` → re-kiq `ingest.pipeline` (R1 — covers TaskIQ message loss and broker outage at POST time).
- `PENDING, updated_at < NOW() - 5 min, attempt ≤ 5` → **stale heartbeat (B16)** ⇒ worker is dead or hung ⇒ re-kiq `ingest.pipeline` (idempotent key: `document_id + attempt`). A live worker keeps its row's `updated_at` fresh and is never re-dispatched.
- `PENDING, updated_at < NOW() - 5 min, attempt > 5` → `FAILED` (cleans chunks/ES per §3.1 R5 path) + structured-log `event=ingest.failed`.
- `DELETING > 5 min` → resume cascade delete idempotently.
- **Multi-READY invariant repair (R3):** every cycle also runs `SELECT source_id, source_app FROM documents WHERE status='READY' GROUP BY source_id, source_app HAVING COUNT(*) > 1` and re-enqueues `ingest.supersede` for each pair.
- **Heartbeat (R8):** every tick increments `reconciler_tick_total` and emits `event=reconciler.tick`. Prometheus alert fires if no tick observed for > 10 min (Reconciler is itself a single point of failure).

**BDD:**
- **S2** Given a `PENDING` document older than 5 min with `attempt ≤ 5`, When the reconciler runs, Then it re-kiqs `ingest.pipeline` exactly once per cycle (idempotent across redelivery).
- **S3** Given a `PENDING` document with `attempt > 5`, When the reconciler runs, Then status transitions to `FAILED`, partial output is cleaned, and a structured log line `event=ingest.failed` is emitted.
- See also S24 (UPLOADED orphan), S26 (multi-READY repair), S30 (heartbeat).

**Infrastructure (B27):** Redis broker (TaskIQ) and Redis rate-limiter are **separate logical instances**, each independently configurable as **standalone or Sentinel** via `REDIS_MODE` env (default `standalone` for dev/CI, set `sentinel` in prod). Sentinel mode shares a single sentinel quorum (`REDIS_SENTINEL_HOSTS`, ≥ 3 nodes) and resolves each instance by its master name (`REDIS_BROKER_SENTINEL_MASTER`, `REDIS_RATELIMIT_SENTINEL_MASTER`). Standalone mode reads direct URLs (`REDIS_BROKER_URL`, `REDIS_RATELIMIT_URL`). Connection layer uses `redis-py-sentinel` when mode=sentinel, plain `redis-py` when mode=standalone. The same code path is used by both the API process and the worker.

#### 3.6.1 Chaos drill suite (P2.6 軌三 / T7.4.x)

The chaos suite asserts the resilience claims of §3.6 (reconciler recovery, idempotent retries, partial-failure tolerance) hold under realistic injected faults. Each case is its own e2e file under `tests/e2e/test_chaos/test_C<N>_<scenario>.py`, marked `@pytest.mark.docker`, gated by a nightly CI lane (not per-PR; injection drills are slow). Case matrix (B49):

| # | Case | Injection point | Expected terminal state |
|---|---|---|---|
| **C1** | Worker `SIGKILL` after `PENDING` transition | `os.kill(worker_pid, SIGKILL)` once status flips to `PENDING` | Reconciler re-dispatch → `READY` ≤ `RECONCILER_PENDING_STALE_SECONDS + RECONCILER_TICK_INTERVAL_SECONDS + worker_pipeline_p99 + slack`; `reconciler_tick_total` increments; no orphan ES chunks |
| **C2** | MariaDB commit ↔ ES bulk crash | Monkeypatch worker to raise `ConnectionError` between DB `commit` and ES `bulk` | Worker retries idempotently; final state `READY` with ES chunks present; `multi_ready_repaired_total` unchanged (no demote needed) |
| **C3** | ES bulk 207 partial failure | WireMock returns ES `_bulk` response with `errors:true` and 5/50 items failed | Worker retries failed items only (idempotent OVERWRITE); `READY` with all 50 chunks; `event=es.bulk_partial_failure` log emitted |
| **C4** | Rerank 5xx during chat | WireMock `/rerank` returns 500 for 3 consecutive calls | Chat returns `200` with RRF-ordered sources (fail-open behaviour, decision to be pinned by P2.3 reranker-wiring commit); `rerank_degraded_total{reason="5xx"}+=3` |
| **C5** | LLM stream interrupt mid-response | WireMock streams 3 `delta` events then drops TCP connection | Server emits `data: {"type":"error","error_code":"LLM_STREAM_INTERRUPTED",...}` per B6; client connection closes cleanly; no 500 in API logs |
| **C6** | MinIO 503 during worker download | WireMock proxy injects 503 on `GET /staging/{key}` for 2/3 attempts | Worker retries (3×@2s built-in); succeeds on attempt 3; `READY`; `minio.transient_error` log count = 2 |

**Common asserts** (every case): `documents.status` reaches terminal value; ES chunks match DB (no orphans); per-case OTEL spans present; `chaos_drill_outcome_total{case="C<N>", outcome="pass"}` increments.

---

### 3.7 Observability

- Haystack auto-trace + FastAPI OTEL middleware → Tempo + Prometheus.
- Structured logs for state-machine transitions; `auth_mode=open` field in P1.
- **Heartbeat metrics (R8):** `reconciler_tick_total` (counter); Prometheus alert when missing > 10 min. Worker emits `worker_pipeline_duration_seconds` (histogram) and `event=ingest.{started,failed,ready}`.
- **Orphan/leak counters:** `minio_orphan_object_total` (post-commit cleanup failure), `multi_ready_repaired_total` (Reconciler R3 sweep).
- **ES events (B26):** `event=es.bbq_unsupported` (cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW); `event=schema.drift` (resource file ↔ live mapping mismatch). Both surface in `/readyz` as degraded (B4).
- **Structured logging (structlog).** JSON to stdout. Categories: (1) **API trace** (`api.request/error`) — per-request `{request_id, method, path, status_code, duration_ms, user_id, trace_id}` via `RequestLoggingMiddleware` (excl. /livez, /readyz, /metrics). (2) **Business** — `chat.retrieval/llm`, `ingest.failed/ready`, `reconciler.tick`, `embedding/rerank.call`, etc., paired with OTEL spans sharing `trace_id`. (2a) **Per-step pipeline** (T2v.42/T-APL.7) — `ingest.step.{started,ok,failed}` and `retrieve.step.{started,ok,failed}` carry `{step, duration_ms, atoms_in?, chunks_out?, error_code?}` plus inherited contextvars (`request_id`/`user_id` from middleware, `document_id`/`mime_type` from the ingest worker). Emitted by every Haystack component wrapped via `wrap_pipeline_component(*, namespace, step)`. After each successful step that returns documents, a companion `{namespace}.step.ok.docs` event is emitted with field `doc_refs: [{document_id, chunk_id, score}]` for every output document (field named `doc_refs` not `documents` to avoid the privacy denylist) — enabling per-chunk tracing across ES search, hydration, reranking, and truncation without re-instrumenting individual components. **Dual emission (T-APL.11):** the same wrapper also opens an OTEL span named `{namespace}.step.{step}` with attributes `{pipeline.namespace, pipeline.step, atoms_in?, chunks_out?, duration_ms}` so traced deployments get waterfall visibility without re-instrumentation; un-traced processes (no `setup_tracing()`) hit the NoOp tracer and pay only context-manager enter/exit cost. **Cross-process correlation (T-APL.9):** `request_id` and `user_id` propagate from the api process across the TaskIQ enqueue/execute seam via `StructlogContextMiddleware` (snapshot to `message.labels` on `pre_send`, rebind on `pre_execute`) so worker-side `ingest.*` logs carry the originating HTTP request's id. (3) **Error** — `error_type, error_code`, traceback, redacted. Format: ISO 8601 UTC; `LOG_FORMAT=console` for dev. **Privacy:** identity + metric fields only; denylist processor drops `query/prompt/messages/completion/chunks/embedding/documents/body/authorization/cookie/password/token/secret` and stamps `content_redacted=true`. `HAYSTACK_CONTENT_TRACING_ENABLED` pinned off.

---

### 3.8 MCP Tool Server (P2.5)

Exposes ragent's retrieval pipeline as an MCP tool (JSON-RPC 2.0, retrieve-only).

> Full spec: [docs/spec/mcp_server.md](spec/mcp_server.md) — protocol, methods, `retrieve` tool schema, error codes, BDD S58–S67.

### 3.9 MCP Hub Microservice

Standalone FastMCP service that federates arbitrary third-party REST APIs as MCP tools.

> Full spec: [docs/spec/mcp_hub.md](spec/mcp_hub.md) — transport, env vars, tools.yaml schema, header forwarding, metrics.

---

## 4. Inventories

### 4.1 Endpoints

> **v2 OVERRIDE for `POST /ingest`** — JSON body only (no multipart).
> ```jsonc
> // ingest_type=inline
> { "ingest_type":"inline", "mime_type":"text/markdown", "content":"# Title\n…",
>   "source_id":"DOC-1", "source_app":"confluence", "source_title":"Q3 OKR",
>   "source_meta":"eng",              // optional, free-format ≤ 1024
>   "source_url":"https://wiki/…" }   // optional, opaque ≤ 2048
> // ingest_type=file
> { "ingest_type":"file", "mime_type":"text/html",
>   "minio_site":"tenant-eu-1", "object_key":"reports/2025.html",
>   "source_id":"DOC-2", "source_app":"s3-importer", "source_title":"Annual Report",
>   "source_meta":"finance", "source_url":"https://…" }
> ```
> Validation order: discriminator-shape (422) → `mime_type ∈ {text/plain,text/markdown,text/html}` (415) → inline `len(content.encode("utf-8")) ≤ INGEST_INLINE_MAX_BYTES` / file HEAD-probe size ≤ `INGEST_FILE_MAX_BYTES` (413) → `minio_site` resolved against `MinioSiteRegistry` (422 `INGEST_MINIO_SITE_UNKNOWN`) → file HEAD-probe object exists (422 `INGEST_OBJECT_NOT_FOUND`). Worker-side guards run before splitter parse: DOCX/PPTX zip preflight (`INGEST_MAX_ARCHIVE_MEMBERS` / `_RATIO` / `_EXPANDED_BYTES`) → 413 `INGEST_ARCHIVE_UNSAFE` persisted as `documents.error_code` with terminal `FAILED`; PDF page-count cap (`INGEST_MAX_PDF_PAGES`) → 413 `INGEST_PDF_TOO_MANY_PAGES` likewise. Every guard rejection increments `ragent_ingest_rejected_total{reason}` (T-SEC.7).

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
| `INTERNAL_ERROR`                     | 500         | Global handler fallback — exception carries no `error_code` | Global exception handler |
| `UPSTREAM_ERROR`                     | 502         | Generic upstream service failure (base-class default; production callers pass a service-specific code below) | `UpstreamServiceError` base |
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
| `INGEST_VALIDATION`                  | 422         | Missing/empty `source_id` / `source_app` / `source_title` (S23) — `errors[]` lists offending fields | Router T2.13 |
| `INGEST_MINIO_SITE_UNKNOWN`          | 422         | `minio_site` not in `MinioSiteRegistry` | Router T2.13 |
| `INGEST_OBJECT_NOT_FOUND`            | 422         | `(minio_site, object_key)` HEAD-probe miss | Router T2.13 |
| `INGEST_NOT_FOUND`                   | 404         | `GET /ingest/v1/{id}` / `DELETE /ingest/v1/{id}` / `POST /ingest/v1/{id}/rerun` on unknown id | Service T2.10 |
| `INGEST_NOT_RERUNNABLE`              | 409         | `POST /ingest/v1/{id}/rerun` on a document whose status is `READY` or `DELETING` (re-POST is the supersede path for READY; DELETING is mid-cascade) | Router (rerun endpoint) |
| `MISSING_USER_ID`                    | 422         | User-id header absent or empty after JWT verification (identity middleware) | Identity middleware |
| `CHAT_RATE_LIMITED`                  | 429 + `Retry-After` | Per-user fixed-window quota exceeded on `/chat/v1` or `/chat/v1/stream` (B31, S37) | Router-level Depends T3.16 |
| `EMBEDDING_LIFECYCLE_INVALID_STATE`  | 409         | Embedding model state-machine transition rejected (B50) | Embedding lifecycle router |
| `EMBEDDING_CUTOVER_PREFLIGHT_FAILED` | 409         | Cutover preflight (warmup / similarity gate) failed (B50) | Embedding lifecycle router |
| `EMBEDDING_INVALID_CONFIG`           | 422         | Invalid promote payload (B50) | Embedding lifecycle router |
| `EMBEDDING_FIELD_NAME_COLLISION`     | 422         | Field name collision with a still-mapped retired field (B50) | Embedding lifecycle router |
| `EMBEDDING_REGISTRY_NOT_READY`       | 503         | Embedding registry not ready for queries | Embedding lifecycle router |
| `FEEDBACK_TOKEN_INVALID`             | 401         | HMAC mismatch, malformed token, or `shown_source_ids` doesn't match the signed `sources_hash` (T-FB.6, B55) | Router (feedback) |
| `FEEDBACK_TOKEN_EXPIRED`             | 410         | Token `ts` outside the 7-day window (T-FB.6, B55) | Router (feedback) |
| `FEEDBACK_SOURCE_INVALID`            | 422         | `source_id ∉ shown_source_ids` (T-FB.6) | Router (feedback) |
| `FEEDBACK_VALIDATION`                | 422         | Schema violations: vote ∉ {±1}, reason outside B56 enum, missing required field | Schema (feedback) |
| `LLM_ERROR`                          | 502 / SSE-error | Pre-stream LLM failure (problem+json) or mid-stream LLM failure (`data: {type:error}`, B6) | Router T3.10/T3.12 |
| `MCP_PARSE_ERROR`                    | JSON-RPC `-32700` | Request body is not valid JSON (S64) | Router P2.5 |
| `MCP_INVALID_REQUEST`                | JSON-RPC `-32600` | Missing `jsonrpc:"2.0"` / `method`; malformed envelope | Router P2.5 |
| `MCP_METHOD_NOT_FOUND`               | JSON-RPC `-32601` | Method outside §3.8.2 allow-list (S61) | Router P2.5 |
| `MCP_TOOL_NOT_FOUND`                 | JSON-RPC `-32602` (data.error_code) | `tools/call` with unknown `name` (S62) | Router P2.5 |
| `MCP_TOOL_INPUT_INVALID`             | JSON-RPC `-32602` (data.error_code) | `tools/call` arguments fail `inputSchema` validation (S63) | Router P2.5 |
| `MCP_TOOL_EXECUTION_FAILED`          | JSON-RPC `-32001` (data.error_code) | Underlying retrieval pipeline raises (S67) | Router P2.5 |
| `ES_PLUGIN_MISSING`                  | 503 (`/readyz`) | ES cluster missing `analysis-icu` plugin (B26, T0.8g) | Bootstrap / readyz |
| `ES_INDEX_MISSING`                   | 503 (`/readyz`) | A `resources/es/*.json` index is absent at boot | Bootstrap / readyz |
| `SCHEMA_DRIFT`                       | 503 (`/readyz`) + log `event=schema.drift` | Live schema differs from `schema.sql` / `resources/es/` | Bootstrap |
| `PIPELINE_TIMEOUT_AGGREGATE`         | `documents.error_code` (TaskErrorCode) | 300 s wall-clock timeout on the full pipeline run; written by the worker and polled via `GET /ingest/v1/{id}` (B18, S34) | Worker T3.2j |
| `PIPELINE_UNROUTABLE`                | `documents.error_code` (TaskErrorCode) | MIME → splitter has no registered route | Worker pipeline |
| `CHUNK_BUDGET_EXCEEDED`              | `documents.error_code` (TaskErrorCode) | `CHUNK_MAX_PIECES_PER_ATOM` exceeded during chunking | `_BudgetChunker` |
| `ES_WRITE_ERROR`                     | `documents.error_code` (TaskErrorCode) | `DocumentWriter` raised during ES bulk write | Worker pipeline |
| `EMBEDDER_ERROR`                     | `documents.error_code` (TaskErrorCode) | Embedder client raised inside the pipeline step | Worker pipeline |
| `PIPELINE_UNEXPECTED_ERROR`          | `documents.error_code` (TaskErrorCode) | Catch-all for any unexpected exception in a pipeline step not tagged with a specific code | Worker pipeline |
| `PIPELINE_MAX_ATTEMPTS_EXCEEDED`     | `documents.error_code` (TaskErrorCode) | Reconciler swept a document stuck in `PENDING` past max retry attempts | Reconciler |
| `PROBE_TIMEOUT`                      | 503 (`/readyz`) | Per-component probe timed out | Bootstrap / readyz |
| `DEPENDENCY_DOWN`                    | 503 (`/readyz`) | A required dependency is unreachable | Bootstrap / readyz |
| `METRICS_DB_UNAVAILABLE`             | 503 (`/readyz`) | Metrics DB unavailable | Bootstrap / readyz |
| `AUTH_TOKEN_EXPIRED`                 | 401             | JWT `exp` claim is in the past (raised through joserfc's verification path, T8.1a) | Auth middleware T8.2a |
| `AUTH_CLAIM_MISSING`                 | 401             | `<RAGENT_JWT_CLAIM_USER_ID>` claim absent or empty after JWKS verification (T8.1a) | Auth middleware T8.2a |
| `AUTH_TOKEN_INVALID`                 | 401             | JWT header absent, token malformed, signature mismatch, wrong `iss`, wrong `aud`, or any other JWKS verification failure outside expiry/missing-claim (T8.1a) | Auth middleware T8.2a |

**Operational events (not `error_code` values — never appear in API responses):**

| Signal | Surface | When |
|---|---|---|
| `es.bbq_unsupported` | structured log `event=es.bbq_unsupported` | Cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW (B26) |
| `reconciler_tick_total` stale | Prometheus alert | `reconciler_tick_total` flat > 10 min (R8, S30) — alerting rule T7.1a |

> **Chat validation (422 without custom `error_code`):** `messages` absent/empty, `provider` outside allow-list, and `source_app`/`source_meta` filter constraint violations are rejected by Pydantic schema validation and return a standard 422 `problem+json` with `errors[]` field details — they do not emit a named `error_code` and are not listed in `HttpErrorCode`.

### 4.2 Supported Formats

| Format | Converter | MIME (allow-list) | Notes | Phase |
|---|---|---|---|:---:|
| `.txt`  | `TextFileToDocument`     | `text/plain`              | UTF-8 text | **P1** |
| `.md`   | `MarkdownToDocument`     | `text/markdown`           | front-matter stripped | **P1** |
| `.html` | `HTMLToDocument`         | `text/html`               | visible text, script/style stripped | **P1** |
| `.csv`  | `CSVToDocument`          | `text/csv`                | row-as-document; rows packed by `RowMerger` to ~2 000 chars (B24); bounded by global 50 MB file limit (B2) | **P1** |
| `.pdf`  | `_PdfASTSplitter`        | `application/pdf`         | per-page `pymupdf4llm.to_markdown` → `_MarkdownASTSplitter`; RapidOCR auto-selected for image-bearing pages; structured atoms (headings, tables, paragraphs); `INGEST_PDF_MARGIN_PTS` clips header/footer zones | **P1** |
| `.docx` | `_DocxASTSplitter`       | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | paragraphs + tables (python-docx) | **P1** |
| `.pptx` | `_PptxASTSplitter`       | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | one atom per slide (python-pptx); footer/date/slide-number placeholders excluded | **P1** |
| `.xlsx` | `XLSXToDocument`         | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | active sheets | P2 |

> 415 on unsupported MIME; 413 on > 50 MB. PDF ingest uses `pymupdf4llm.to_markdown` per page to produce structured markdown atoms (headings, tables, paragraphs); `rapidocr-onnxruntime` is auto-selected by pymupdf4llm for image-bearing pages (no OS-level dependency). If `to_markdown` raises, the splitter falls back to `page.get_text("text")` and logs a warning — the page is still ingested as plain text. `INGEST_PDF_MARGIN_PTS` (default `0`) clips that many PDF points from the top and bottom of each page, excluding header/footer zones. PPTX footer, date, and slide-number placeholders are always excluded regardless of setting.

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

**TokenManager refresh discipline (P-F):** each `TokenManager` instance has its own `threading.Lock`; concurrent callers around the `expiresAt − 5 min` boundary share one in-flight refresh per manager. Local mode: three independent managers (`AI_LLM/EMBEDDING/RERANK_API_J1_TOKEN`), each caching its own J2. K8s mode (`AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true`): one shared manager reads the SA token file per refresh and its J2 is shared across all three clients.

### 4.6 Environment Variables (C2 + B28)

> Full inventory: [`docs/spec/env_vars.md`](spec/env_vars.md)
>
> **Rule (B28):** every external dependency, timeout, threshold, and credential MUST appear in that file.  
> Code reading a literal not listed there is a spec drift bug.  
> `.env.example` drift test (`tests/unit/test_env_example_drift.py`) gates symmetry.

---

## 5. Data Structures

> Full schemas: [`docs/spec/data_structures.md`](spec/data_structures.md)

MariaDB tables: `documents`, `feedback`, `system_settings`. ES indexes: `chunks_v1` (content + embeddings), `feedback_v1`. ID format: UUIDv7 → 26-char Crockford Base32.

---

## 6. Standards

> Full standards: [`docs/spec/standards.md`](spec/standards.md)

Schema migrations (Alembic + `schema.sql` snapshot), module layout, boot auto-init, API naming, test organisation.

---

## 7. Decision Log

> Full log: [`docs/spec/decision_log.md`](spec/decision_log.md)

Append-only design decisions (B1–B60). Each row: ID · Date · Domain · Question · Decision · Alternatives rejected · Affects.  
New decisions require a **new dated row**; never edit in place.
