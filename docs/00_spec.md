# 00_spec.md — Distributed RAG Agent

> Source: `docs/draft.md` · Standard: `docs/00_rule.md`

---

## 1. Mission

- Enterprise internal knowledge retrieval backend.
- Streaming chat answers grounded in private documents.
- Pluggable extractor architecture: graph reasoning (P3) without pipeline rewrite.

### ⚠️ P1 OPEN Mode
- Authentication **DISABLED** in P1. `X-User-Id` header trusted; recorded as `documents.create_user` (audit only, not authorization). JWT restored in **P2**.
- Permission gating **DISABLED** in P1. The Permission Layer (§3.5) ships in **P2**, backed by OpenFGA, and stays out of the retrieval/ES path.
- Startup guard refuses to start unless `RAGENT_AUTH_DISABLED=true AND RAGENT_ENV=dev`.

---

## 2. Phase 1 Scope

| In P1 | Deferred |
|---|---|
| Ingest CRUD (Create / Read / List / Delete) with cascade | JWT → P2 |
| Indexing Pipeline (§3.2) + Chat Pipeline (§3.4) | AsyncPipeline → P2 |
| Plugin Protocol v1, VectorExtractor, StubGraphExtractor | GraphExtractor → P3 |
| Third-party clients: Embedding, LLM, Rerank, TokenManager | Rerank wiring → P2 |
| Reconciler + locking | MCP real handler → P2 |
| Observability: OTEL auto-trace | — |

---

## 3. Domains

### 3.1 Ingest Lifecycle

**State machine:** `UPLOADED → PENDING → READY | FAILED`; `DELETING` transient on delete.

**Locking discipline:**
- Status mutations use **two short transactions**, not one long one:
  - **TX-A** `SELECT … FOR UPDATE NOWAIT` → write `PENDING`/terminal status → **commit** (releases row lock).
  - **Pipeline body runs OUTSIDE any DB transaction.** No row locks are held while external calls (embedder, ES, plugins, MinIO) run — this prevents pipeline hangs from blocking the Reconciler's `SKIP LOCKED` sweep.
- Worker uses `FOR UPDATE NOWAIT`: on lock contention (e.g. concurrent dispatch by Reconciler) the worker fails fast and re-kiqs itself instead of blocking on `innodb_lock_wait_timeout`.
- Reconciler uses `FOR UPDATE SKIP LOCKED`.
- `update_status` validates the state machine and raises `IllegalStateTransition` on invalid transitions.

**Worker heartbeat (B16) — closes the no-lock-window race:** because the pipeline body holds no row lock, a naive Reconciler "PENDING > 5 min" sweep would happily re-dispatch a still-running worker and produce double processing. The worker therefore **updates `documents.updated_at = NOW()` every 30 s** during the pipeline body (background timer; one cheap PK-keyed `UPDATE`). The Reconciler's threshold becomes `WHERE status='PENDING' AND updated_at < NOW() - INTERVAL 5 MINUTE` — only **stale-heartbeat** rows are re-dispatched. Heartbeat interval is configured via `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30).

**Per-document pipeline timeout (B18):** the worker enforces an overall ceiling of `PIPELINE_TIMEOUT_SECONDS` (default 1800 s = 30 min) around the pipeline body. On overrun, the worker transitions the row to `FAILED` with `error_code=PIPELINE_TIMEOUT` and runs the §3.1 R5 cleanup path (`fan_out_delete` + `delete_by_document_id`). This bounds large-CSV / pathological-document worst case so the Reconciler never sees an infinitely-running worker (heartbeat would also catch it after 5 min, but the ceiling makes the failure deterministic).

**Storage model:** MinIO is **transient staging only** — needed because Router (API) and Worker may run on different hosts. The original file is deleted from MinIO after the pipeline reaches a terminal state (`READY` or `FAILED`). After ingest, only chunks (ES) + metadata (MariaDB) remain.

**Object key convention (B10):** `{source_app}_{source_id}_{document_id}` (single bucket from `MINIO_BUCKET` env, default `ragent`). `source_app` and `source_id` are sanitized to `[A-Za-z0-9._-]` (other chars percent-encoded) to satisfy MinIO key constraints. The `document_id` suffix guarantees uniqueness even when the same `(source_app, source_id)` is re-POSTed before supersede converges.

**Pipeline retry idempotency:** Every pipeline run begins with `ChunkRepository.delete_by_document_id(document_id)` and `VectorExtractor.delete(document_id)` (idempotent ES bulk-delete) so a Reconciler retry of a partially-written attempt does not produce duplicate chunks. `chunk_id` may therefore be a fresh `new_id()` per run; identity is by `(document_id, ord)`.

**Supersede model (smart upsert):** Every `POST /ingest` carries a mandatory `(source_id, source_app, source_title)` triple (e.g. `("DOC-123", "confluence", "Q3 OKR Planning")`) and an optional `source_workspace`. The `(source_id, source_app)` pair is the **logical identity** of a document; `source_title` is human-readable display text required by chat retrieval (`sources[].title` in §3.4). At steady state at most one `READY` row may exist per `(source_id, source_app)`. A new POST always creates a fresh `document_id`; when it reaches `READY`, the system enqueues a **supersede** task that selects every `READY` row sharing the same `(source_id, source_app)`, keeps the one with `MAX(created_at)`, and cascade-deletes the rest. This guarantees "latest write wins" even when documents finish out-of-order, gives zero-downtime replacement (old chunks remain queryable until the new ones are indexed), and preserves the old version if the new ingest fails. Supersede is enqueued **only** on the `PENDING → READY` transition; FAILED or mid-flight DELETE never triggers it. Uniqueness is **eventual**, enforced by supersede — not by a DB UNIQUE constraint, since transient duplicates are expected during ingestion. **Mutation = re-POST with the same `(source_id, source_app)` (and updated `source_title` if the title changed); there is no PUT/PATCH endpoint.**

**Create flow:**
1. `POST /ingest` (`source_id`, `source_app`, `source_title`, optional `source_workspace`) → MIME/size validation → MinIO upload (staging) → `documents(UPLOADED, source_id, source_app, source_title, source_workspace)` → kiq `ingest.pipeline` → 202.
2. Worker `ingest.pipeline`:
   - **TX-A:** `acquire FOR UPDATE NOWAIT`. On lock contention → re-kiq self with backoff and exit (no attempt increment). On success → `PENDING`, `attempt+1`, **commit**.
   - **Heartbeat (B16):** start a background timer that issues `UPDATE documents SET updated_at=NOW() WHERE document_id=?` every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30) for the lifetime of the pipeline body. Cancelled in `finally` before TX-B.
   - **Pipeline body (no DB tx, wall-clock-bounded by `PIPELINE_TIMEOUT_SECONDS`, default 1800):** `delete_by_document_id` (idempotency) → run §3.2 → `fan_out` → all required ok ⇒ outcome=`READY`; any required error ⇒ outcome=`PENDING_RETRY` (no terminal commit; Reconciler resumes); attempt > 5 ⇒ outcome=`FAILED`; ceiling exceeded ⇒ outcome=`FAILED` with `error_code=PIPELINE_TIMEOUT` (B18).
   - **TX-B (terminal only):** commit `READY` or `FAILED`. **On `FAILED`, also call `fan_out_delete` + `delete_by_document_id` to clean partial output before commit.**
   - **Post-commit (best-effort, no tx):** `MinIOClient.delete_object` (errors swallowed → log `event=minio.orphan_object`); on `READY`, kiq `ingest.supersede(document_id)` (idempotent).
3. Worker `ingest.supersede`:
   - **Single-loser-per-tx:** in a loop, `SELECT 1 row` matching `(source_id, source_app, status='READY')` ordered ASC by `created_at` (i.e. the oldest non-survivor) using `FOR UPDATE SKIP LOCKED` → cascade-delete that row → commit → repeat. Survivor = whichever row remains last; query naturally stops when only one `READY` row is left for the pair. **Avoids holding K row-locks across K cascades.**
   - Naturally idempotent: re-delivery finds ≤ 1 row and no-ops.

**Delete flow:**
1. `DELETE /ingest/{id}` → `acquire FOR UPDATE NOWAIT` → `DELETING` (commit short tx) → outside-tx: `fan_out_delete` → `delete_by_document_id` → if status was `PENDING/UPLOADED` also `MinIO.delete_object` → final tx: delete row → 204.
2. Any mid-cascade failure → row stays `DELETING`; Reconciler resumes idempotently.

**BDD:**
- **S1** POST 1 MB `.txt` → 202 + 26-char task_id; status → `READY` within 60 s; chunks in ES.
- **S10** Illegal transitions (e.g. `READY→PENDING`) raise `IllegalStateTransition`.
- **S12** DELETE cascade on `READY` doc → `DELETING` → all plugins called once → ES/row cleared → 204 (MinIO already cleared at READY).
- **S13** Any failure mid-delete → row stays `DELETING`; Reconciler resumes ≤ 5 min.
- **S16** Pipeline reaches terminal state (`READY` or `FAILED`) → MinIO object deleted; subsequent re-processing not possible without re-upload.
- **S14** Re-DELETE an already-deleted document → 204, no plugin calls.
- **S15** `GET /ingest?limit=2` on 5 docs → ≤ 2 items + `next_cursor` continues.
- **S17 supersede happy path** — Given a `READY` doc D1 with `(source_id="X", source_app="confluence")`, When client POSTs another file with the same pair, Then a new doc D2 is created; while D2 is `PENDING`, queries still see D1 chunks; when D2 reaches `READY`, supersede task cascade-deletes D1; queries now see only D2 chunks.
- **S18 supersede on failure preserves old** — Given D1 `READY` with `(source_id, source_app)=("X","confluence")`, When new D2 with the same pair ends up `FAILED`, Then D1 remains `READY` and queryable; supersede task is **not** enqueued.
- **S19 supersede idempotent** — Given supersede already ran for D2, When the task fires again, Then no further deletes occur (only one `READY` row remains for that `(source_id, source_app)`).
- **S20 supersede out-of-order finish** — Given D1 (created at t=0) and D2 (created at t=1) share `(source_id="X", source_app="confluence")`, When D2 reaches `READY` first (D1 still `PENDING`) and D1 reaches `READY` later, Then after both supersede tasks run only D2 (the row with `MAX(created_at)`) remains; D1 is cascade-deleted.
- **S22 same source_id different source_app coexist** — Given doc D1 with `(source_id="X", source_app="confluence")` is `READY`, When client POSTs with `(source_id="X", source_app="slack")`, Then both reach `READY` and coexist; supersede touches neither.
- **S23 missing source_id, source_app, or source_title** — Given a POST omits any of `source_id` / `source_app` / `source_title`, Then router returns 422 problem+json with field-level `errors[]`; no MinIO upload, no DB row.
- **S21 worker crash post-commit MinIO orphan tolerated** — Given the worker has committed `status=READY` but crashed before `MinIO.delete_object` returned, When the document is fetched, Then status is `READY` and the orphan staging object is logged as `event=minio.orphan_object` (acceptable; P2 sweeper will GC).
- **S24 UPLOADED orphan recovered (R1)** — Given a row in `UPLOADED` for > 5 min (TaskIQ message lost or broker outage at POST time), When the Reconciler runs, Then it re-kiqs `ingest.pipeline` and the doc proceeds normally.
- **S25 pipeline retry produces no duplicate chunks (R4)** — Given a Reconciler retry of a previously partially-written ingest, When the pipeline reruns, Then `chunks` count for that `document_id` equals the chunker's output and `chunks_v1` ES index has no orphans.
- **S26 multi-READY invariant repaired (R3)** — Given two `READY` rows for the same `(source_id, source_app)` (e.g. supersede task lost between status commit and kiq), When the Reconciler runs, Then it re-enqueues `ingest.supersede` and convergence happens within one cycle.
- **S27 FAILED transition cleans partial output (R5)** — Given a doc transitions to `FAILED`, When the FAILED state is committed, Then `chunks` and ES `chunks_v1` for that `document_id` have been cleared (no leakage into chat retrieval).
- **S28 worker FOR UPDATE NOWAIT contention (R7)** — Given two workers receive the same `document_id` (initial kiq + Reconciler dispatch), When the second worker tries to acquire, Then it fails fast (`NOWAIT`), re-kiqs itself with backoff, and does not increment `attempt`.
- **S29 plugin call timeout (R6)** — Given a plugin's `extract()` exceeds its declared timeout, When `fan_out` waits, Then the plugin returns `Result(error="timeout")` and the worker treats it as a normal required-plugin error (PENDING + Reconciler retry).
- **S30 reconciler heartbeat (R8)** — Given the Reconciler tick runs, Then a metric `reconciler_tick_total` is incremented and a structured-log `event=reconciler.tick` line is emitted; absence > 10 min triggers Prometheus alert.
- **S31 supersede single-loser-per-tx (P-C)** — Given supersede must delete K=10 losers, When the task runs, Then each loser is deleted in its own committed transaction (loop), not one transaction holding K row-locks.
- **S32 ES freshness window (P-G)** — Given a chunk just indexed, When chat queries within 1 s, Then it may not yet be visible (acceptable per default `refresh_interval=1s`); within 60 s it is queryable (S1 acceptance).
- **S33 worker heartbeat (B16)** — Given a worker has been running the pipeline body for 4 min, When the Reconciler ticks at 5 min, Then it sees `updated_at` was refreshed < 30 s ago and **does not** re-dispatch the row. Conversely, when a worker dies and `updated_at` ages past 5 min, the Reconciler re-dispatches exactly once.
- **S34 pipeline timeout (B18)** — Given a pipeline body runs longer than `PIPELINE_TIMEOUT_SECONDS`, When the ceiling fires, Then the worker transitions the row to `FAILED` with `error_code=PIPELINE_TIMEOUT`, runs cleanup (`fan_out_delete` + `delete_by_document_id`), and emits `event=ingest.failed reason=pipeline_timeout`.
- **S35 CSV row packing (B24)** — Given a CSV with 10 000 short rows (~50 chars each), When ingested, Then `chunks` row count is bounded by `ceil(total_chars / CSV_CHUNK_TARGET_CHARS)` (≈ 250 for the example), not 10 000. Non-CSV formats are unaffected.

---

### 3.2 Indexing Pipeline

```
FileTypeRouter → Converter → [RowMerger (CSV path only)] → DocumentCleaner → LanguageRouter → {cjk_splitter | en_splitter} → EmbeddingClient → ChunkRepository → PluginRegistry.fan_out()
```

```mermaid
flowchart LR
  A[FileTypeRouter] --> B["Converter\n(.txt/.md/.csv/...)"]
  B -->|"MIME == text/csv"| M["RowMerger\n(B24)"]
  B -->|"MIME != text/csv"| C[DocumentCleaner]
  M --> C
  C --> D[LanguageRouter]
  D --> E1["cjk_splitter\n(sentence)"]
  D --> E2["en_splitter\n(sentence, default)"]
  E1 & E2 --> F["EmbeddingClient\nbge-m3"]
  F --> G["ChunkRepository\nbulk_insert"]
  G --> H["PluginRegistry\nfan_out()"]
```

**Haystack 2.x sync Pipeline in P1; AsyncPipeline in P2.**

Pluggable points: (a) per-format converter, (b) per-language splitter, (c) EmbeddingClient, (d) registered extractor plugins.

**CSV row packing (B24):** `CSVToDocument` emits one Document per row. For a 50 MB CSV this is up to ~1 M rows ⇒ ~1 M tiny chunks ⇒ ~31 k embedder batches ⇒ unbounded ingest time. The CSV branch therefore inserts a `RowMerger` SuperComponent that joins consecutive rows with `\n` until the buffered text reaches `CSV_CHUNK_TARGET_CHARS` (default 2 000 chars ≈ 512 tokens, well below bge-m3's 8 192). Each merged buffer becomes one Document; downstream sentence splitting then refines further. Non-CSV formats bypass `RowMerger` (Haystack `ConditionalRouter` based on MIME). Result: 50 MB CSV → ~25 k chunks instead of ~1 M.

**Performance & timeout discipline (R6, P-B, B18):**
- The pipeline's first step is `ChunkRepository.delete_by_document_id` + `PluginRegistry.fan_out_delete` (idempotency for retry — see §3.1; sweeps every plugin, not just `VectorExtractor`).
- `EmbeddingClient` is invoked in **batches of 32 chunks** per HTTP call (configurable; never 1-by-1).
- Every external call carries an explicit timeout: Embedder 30 s/batch (ingest), ES bulk 60 s, MinIO get 30 s, plugin `extract()` 60 s overall (enforced by `PluginRegistry.fan_out`).
- **Overall pipeline ceiling:** `PIPELINE_TIMEOUT_SECONDS` (default 1800 s, B18). Overrun ⇒ `FAILED` with `error_code=PIPELINE_TIMEOUT`.
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
QueryEmbedder → {ESVectorRetriever (kNN on `embedding`, optional filter) ∥ ESBM25Retriever (multi_match on `["text", "title^2"]`, optional filter)} → DocumentJoiner(RRF) → SourceHydrator(JOIN documents) → LLMClient.{chat | stream}
```

Title participates in **both** retrieval surfaces (B15): semantic (baked into every chunk's `embedding` at ingest via `embed(f"{title}\n\n{text}")`) and lexical (BM25 boosted 2× via `multi_match`). No separate title-only retriever, no extra ES field beyond `title`.

**Filter scope (B29):** Optional `source_app` / `source_workspace` request params (§3.4.1) translate to ES `term` filters applied to both retrievers' `filter` clause. Both fields denormalised onto chunks at ingest (§5.2). Empty filter ⇒ unrestricted retrieval (current P1 behaviour). Both filters AND together when both are supplied.

**Two endpoints (B12):**
- `POST /chat` → non-streaming, returns full JSON body once LLM completes.
- `POST /chat/stream` → SSE; `delta` events stream incremental `content`, closing with one `done` event whose data payload is the same JSON body as `/chat`.

Both share the same request schema and the same retrieval pipeline; only the LLM call differs (`LLMClient.chat` vs `LLMClient.stream`).

**Join mode toggle (C6):** `DocumentJoiner` is config-driven via `CHAT_JOIN_MODE` env var:

| Mode | Pipeline shape |
|---|---|
| `rrf` (default) | both retrievers + `DocumentJoiner(join_mode="reciprocal_rank_fusion")` (`k=60`) |
| `concatenate` | both retrievers + simple merge (no re-ranking) |
| `vector_only` | `ESVectorRetriever` only; no joiner |
| `bm25_only` | `ESBM25Retriever` only; no joiner |

The factory assembles the appropriate component graph at startup; the chat router has no knowledge of the mode. Switching is a pure-config operation, no code change.

**P1 OPEN:** no permission gating; `sources[]` unrestricted. Future phase introduces a **Permission Layer post-filter** on retrieved chunks (§3.5) — ES queries themselves remain permission-blind in every phase.

**Performance note (P-A):** Haystack sync Pipeline in P1 runs `ESVectorRetriever` and `ESBM25Retriever` **sequentially**, so chat latency = `vector + BM25 + LLM` (additive). True parallelism requires AsyncPipeline (P2) or wrapping the two retrievers in a custom `ThreadPoolExecutor` SuperComponent. P1 ships sequential and accepts the latency hit; P2 makes them concurrent.

#### 3.4.1 Request schema (shared by `/chat` and `/chat/stream`)

```json
{
  "messages":         [{"role": "system|user|assistant", "content": "..."}],
  "provider":         "openai",
  "model":            "gptoss-120b",
  "temperature":      0.7,
  "maxTokens":        4096,
  "source_app":       "confluence",
  "source_workspace": "engineering"
}
```

- Only `messages` is required. All other fields are optional and fall back to the defaults shown above.
- **Retrieval filters (B29):** `source_app` and `source_workspace` are optional. When present, both retrievers apply an ES `term` filter on the matching chunk field (denormalised at ingest, §5.2). Both supplied ⇒ AND. Both omitted ⇒ unrestricted retrieval. Empty string is rejected (422 `error_code=CHAT_FILTER_INVALID`); to skip a filter, omit the field. `source_app` length ≤ 64, `source_workspace` length ≤ 64 (matches §5.1 column widths).
- `maxTokens` caps the LLM output (`completionTokens`); not the prompt.
- If `messages` does not contain a `role:"system"` entry, the server **prepends** `{"role":"system","content":"You are a helpful assistant"}` before invoking the LLM.
- The retrieval query is the **last `role:"user"` message**; preceding messages are passed through to the LLM as conversation history.
- **`provider` validation (B22):** P1 ships a single LLM endpoint (§4.5). The router validates `provider` against the allow-list `{"openai"}` and returns 422 (`error_code=CHAT_PROVIDER_UNSUPPORTED`) on any other value. The accepted value is **echoed verbatim** in the response (§3.4.2); P1 does not route on it. Future phases extend the allow-list and use the field as a routing key.
- Validation errors → 422 RFC 9457 problem+json (B5).

#### 3.4.2 Response schema

`/chat` (non-streaming, `Content-Type: application/json`) and the terminal `done` event of `/chat/stream` both carry:

```json
{
  "content":  "COMPLETE_MARKDOWN_RESPONSE",
  "usage":    {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
  "model":    "gptoss-120b",
  "provider": "openai",
  "sources":  [
    {
      "document_id": "01J9...",
      "source_app":  "confluence",
      "source_id":   "DOC-123",
      "type":        "knowledge",
      "title":       "Q3 OKR Planning",
      "excerpt":     "...chunk text snippet..."
    }
  ]
}
```

- `sources` is `null` when no chunks were retrieved (e.g. empty index, retriever error fallback). Otherwise every entry has **all fields populated** — partial rows are not emitted.
- `sources[].type` is reserved for future categorisation; **P1 always emits `"knowledge"`**. Future values `"app"` and `"workspace"` will be derived in a later phase.
- `sources[].title` comes from `documents.source_title` (joined on `document_id` after retrieval).
- `sources[].excerpt` is the chunk text the retriever surfaced. **Truncated to `EXCERPT_MAX_CHARS` (default 512) inside `SourceHydrator`** (B23) — single locus, before the LLM sees it. Truncation appends `…` if cut. Router never re-truncates.
- `usage` is reported by `LLMClient`; for streaming the LLM API must include final usage (e.g. OpenAI `stream_options.include_usage=true`).

#### 3.4.3 Streaming wire format (`/chat/stream` only)

```
data: {"type":"delta","content":"<token chunk>"}\n\n
…
data: {"type":"done", ...response schema as 3.4.2…}\n\n
```

**Error mid-stream (B6):** If the LLM or any retriever fails *after* the first `delta` has been written, the server emits a single default-event `data:` line with payload `{"type":"error","error_code":"<CODE>","message":"<text>"}` and closes the connection. **No `event: error` named-event is used.** Pre-stream failures (before the first `delta`) return a normal RFC 9457 problem+json response. `/chat` always uses problem+json on error (it has no streaming surface).

**BDD:**
- **S6**  — `POST /chat/stream` emits ≥ 1 `data: {type:"delta",...}` then exactly one `data: {type:"done",...}` carrying `content`, `usage`, `model`, `provider`, `sources`.
- **S6a** — `POST /chat` returns `200 application/json` with the full §3.4.2 body (single response, no streaming framing).
- **S6b** — Request without `role:"system"` entry → server prepends the default system message before LLM invocation; observable via mock LLM capture.
- **S6c** — Request with only `messages` → defaults (`provider="openai"`, `model="gptoss-120b"`, `temperature=0.7`, `maxTokens=4096`) applied.
- **S6d** — Empty retrieval (index empty or retriever error) → response `sources: null` and the LLM still answers.
- **S6e** — Every emitted `sources[]` entry has all six fields populated and `type="knowledge"`.
- **S6f filter narrows by source_app (B29)** — Given indexed chunks span `source_app ∈ {"confluence","slack"}`, When `POST /chat {"messages":[…], "source_app":"confluence"}` runs, Then both retrievers issue ES queries with `term: {source_app: "confluence"}` and `sources[]` contains only `source_app="confluence"` rows.
- **S6g filter AND combination (B29)** — Given chunks across `(source_app, source_workspace)` ∈ {(confluence,eng), (confluence,hr), (slack,eng)}, When `POST /chat {…, "source_app":"confluence", "source_workspace":"eng"}` runs, Then `sources[]` contains only the (confluence, eng) row.
- **S6h filter no-match (B29)** — Given no chunks match the filter, When the request runs, Then `sources` is `null` (§3.4.2 contract for empty retrieval) and the LLM still answers.
- **S6i filter empty string rejected (B29)** — Given `source_app=""`, When the request runs, Then 422 problem+json with `error_code=CHAT_FILTER_INVALID`; no LLM call is made.
- **S36 CJK BM25 via icu_tokenizer (B26)** — Given a document body containing `"產品規格"` (no whitespace) indexed under the `icu_text` analyzer, When a chat query for `"產品規格"` runs against `chunks_v1`, Then the BM25 retriever returns the chunk; the same query against a `standard`-analyzed control index does not. Proves the analyzer choice (B26) is functionally required for CJK retrieval.
- **S8**  — `POST /mcp/tools/rag` returns 501 in P1 (handler not yet wired).

---

### 3.5 Authentication & Permission

Two distinct concerns, kept architecturally separate from retrieval:

| Concern | Question answered | Mechanism | P1 | Future phase |
|---|---|---|---|---|
| **Authentication** | Who is the caller? | JWT verify → `user_id` from subject claim | OFF — `X-User-Id` header trusted, validated non-empty | JWT validated via FastAPI dependency |
| **Permission** | Can this caller see this document? | Permission Layer service that calls **OpenFGA** | OPEN — no checks, all docs visible | `PermissionClient.batch_check(user_id, document_ids)` returns the allowed subset |

**Design principle:** Elasticsearch (`chunks_v1`) carries **no auth fields** in any phase. Retrieval is permission-blind; the Permission Layer post-filters retrieved chunks by their `document_id`. This keeps ES schema stable across phases, avoids the "owner-filter on every chunk" duplication, and lets the permission model evolve (roles, sharing, groups) without re-indexing.

**Permission Layer interface (P2):**

```python
class PermissionClient(Protocol):
    def batch_check(self, user_id: str, document_ids: list[str], relation: str = "viewer") -> set[str]: ...
    def list_objects(self, user_id: str, relation: str = "viewer") -> list[str] | None: ...  # None = "too many, fall back to batch_check"
```

`PermissionClient` is the single integration point for OpenFGA. It is the only module that imports the OpenFGA SDK; everything else depends on the Protocol.

**Chat retrieval gating (P2):**

```
ES retrieval (top K' candidates)
       ↓
PermissionClient.batch_check(user_id, candidate_doc_ids)
       ↓
filter to allowed → SourceHydrator → LLM
```

Retrieval may over-fetch (`K' = K × overfetch_factor`) so that after permission filtering at least K results remain. Strategy and `overfetch_factor` are decided when P2 ships; not P1 concerns.

**P1 (current phase):**
- No JWT — `X-User-Id` is a trusted string, used only for `documents.create_user` (audit) and OTEL span tags.
- No Permission Layer — chat returns all matching chunks, ingest list endpoint is unrestricted.
- Audit logs for destructive ops emitted at INFO with `auth_mode=open`.
- **TokenManager (J1→J2) is active in P1** — used by Embedding/LLM/Rerank clients only (third-party API auth, unrelated to user auth).

**Note on prior decision:** An earlier round declared OpenFGA out-of-scope across all phases (see `00_journal.md`). That decision is **superseded** here: OpenFGA returns as the Permission-Layer backend in P2, but is fully encapsulated behind `PermissionClient` and never reaches the retrieval / data layers. The original concern (no out-of-band ACL on every ES query) is preserved by routing permission checks through a post-retrieval gate, not an ES filter.

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

---

### 3.7 Observability

- Haystack auto-trace + FastAPI OTEL middleware → Tempo + Prometheus.
- Structured logs for state-machine transitions; `auth_mode=open` field in P1.
- **Heartbeat metrics (R8):** `reconciler_tick_total` (counter); Prometheus alert when missing > 10 min. Worker emits `worker_pipeline_duration_seconds` (histogram) and `event=ingest.{started,failed,ready}`.
- **Orphan/leak counters:** `minio_orphan_object_total` (post-commit cleanup failure), `multi_ready_repaired_total` (Reconciler R3 sweep).
- **ES events (B26):** `event=es.bbq_unsupported` (cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW); `event=schema.drift` (resource file ↔ live mapping mismatch). Both surface in `/readyz` as degraded (B4).

---

## 4. Inventories

### 4.1 Endpoints

| Method | Path | P1 Auth | Request | Response |
|---|---|---|---|---|
| POST   | `/ingest`               | `X-User-Id` | `multipart/form-data: file` (≤ 50 MB, MIME ∈ §4.2), `source_id` (≤ 128, **mandatory**), `source_app` (≤ 64, **mandatory**), `source_title` (≤ 256, **mandatory**), `source_workspace` (≤ 64, optional) | `202 { task_id }` — `task_id` **is** the `document_id` (26-char Crockford Base32). 422 problem+json if any of `source_id` / `source_app` / `source_title` missing/empty. |
| GET    | `/ingest/{id}`          | `X-User-Id` | — | `200 { status, attempt, updated_at }` |
| GET    | `/ingest?after=&limit=` | `X-User-Id` | — | `200 { items, next_cursor }` (limit ≤ 100) |
| DELETE | `/ingest/{id}`          | `X-User-Id` | — | `204` idempotent |
| POST   | `/chat`                 | `X-User-Id` | §3.4.1 schema (`messages` required; rest default) | `200 application/json` per §3.4.2 |
| POST   | `/chat/stream`          | `X-User-Id` | §3.4.1 schema | `text/event-stream` per §3.4.3 (`data: {type:delta\|done\|error}`) |
| POST   | `/mcp/tools/rag`        | `X-User-Id` | `{ query: str }` | **501** (P1) |
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
| `INGEST_VALIDATION`                  | 422         | Missing/empty `source_id` / `source_app` / `source_title` (S23) — `errors[]` lists offending fields | Router T2.13 |
| `INGEST_NOT_FOUND`                   | 404         | `GET /ingest/{id}` / `DELETE /ingest/{id}` on unknown id | Service T2.10 |
| `CHAT_MESSAGES_MISSING`              | 422         | `messages` absent or empty | Schema T3.3 |
| `CHAT_PROVIDER_UNSUPPORTED`          | 422         | `provider` outside `{"openai"}` allow-list (B22) | Schema T3.3 |
| `CHAT_FILTER_INVALID`                | 422         | `source_app` or `source_workspace` empty string / exceeds 64 chars (B29) | Schema T3.3 |
| `CHAT_LLM_ERROR`                     | 502 / SSE-error | Pre-stream LLM failure (problem+json) or mid-stream LLM failure (`data: {type:error}`, B6) | Router T3.10/T3.12 |
| `CHAT_RETRIEVER_ERROR`               | 502 / SSE-error | ES vector / BM25 retriever failure | Router T3.10/T3.12 |
| `MCP_NOT_IMPLEMENTED`                | 501         | `POST /mcp/tools/rag` (S8) | Router T6.1 |
| `ES_PLUGIN_MISSING`                  | 503 (`/readyz`) | ES cluster missing `analysis-icu` plugin (B26, T0.8g) | Bootstrap / readyz |
| `ES_INDEX_MISSING`                   | 503 (`/readyz`) | A `resources/es/*.json` index is absent at boot | Bootstrap / readyz |
| `SCHEMA_DRIFT`                       | 503 (`/readyz`) + log `event=schema.drift` | Live schema differs from `schema.sql` / `resources/es/` | Bootstrap |
| `PIPELINE_TIMEOUT`                   | log `event=ingest.failed reason=pipeline_timeout` | Pipeline body exceeds `PIPELINE_TIMEOUT_SECONDS` (B18, S34) | Worker T3.2j |
| `ES_BBQ_UNSUPPORTED`                 | log `event=es.bbq_unsupported` | Cluster rejected `bbq_hnsw`; bootstrap retried with standard HNSW (B26) | Bootstrap |
| `RECONCILER_TICK_MISSING`            | Prometheus alert | `reconciler_tick_total` flat > 10 min (R8, S30) | Alerting rule T7.1a |

### 4.2 Supported Formats

| Format | Converter | MIME (allow-list) | Notes | Phase |
|---|---|---|---|:---:|
| `.txt`  | `TextFileToDocument`     | `text/plain`              | UTF-8 text | **P1** |
| `.md`   | `MarkdownToDocument`     | `text/markdown`           | front-matter stripped | **P1** |
| `.html` | `HTMLToDocument`         | `text/html`               | visible text, script/style stripped | **P1** |
| `.csv`  | `CSVToDocument`          | `text/csv`                | row-as-document; rows packed by `RowMerger` to ~2 000 chars (B24); bounded by global 50 MB file limit (B2) | **P1** |
| `.pdf`  | `PyPDFToDocument`        | `application/pdf`         | text-extractable only | P2 |
| `.docx` | `DOCXToDocument`         | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | body + tables | P2 |
| `.pptx` | `PPTXToDocument`         | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | slide text + notes | P2 |
| `.xlsx` | `XLSXToDocument`         | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | active sheets | P2 |

> 415 on unsupported MIME; 413 on > 50 MB. Image-only / scanned documents are not supported in any phase.

### 4.3 Pipeline Catalog

| Pipeline | Components | Timeouts | Test Path | Phase |
|---|---|---|---|:---:|
| **Ingest** | `delete_by_document_id (idempotency) → FileTypeRouter → Converter → DocumentCleaner → LanguageRouter → {cjk_splitter \| en_splitter} (sentence-level, B1) → EmbeddingClient(bge-m3, batch=32) → ChunkRepository.bulk_insert → PluginRegistry.fan_out (per-plugin 60 s)` | Embedder 30 s/batch · ES bulk 60 s · MinIO get 30 s · plugin 60 s | `tests/integration/test_ingest_pipeline.py` | **P1** sync |
| **Chat** | `QueryEmbedder → ESVector(kNN on `embedding`, `bbq_hnsw` index, optional `term` filter on `source_app`/`source_workspace` — B29) → ESBM25(multi_match `text`+`title^2`, `icu_text` analyzer, B26, same optional filter) → DocumentJoiner (C6 `CHAT_JOIN_MODE`: rrf\|concatenate\|vector_only\|bm25_only) → SourceHydrator(JOIN documents, truncate `excerpt` to 512 chars) → LLMClient.{chat\|stream}` (retrievers sequential in P1; parallel in P2 — see §3.4 P-A) | Embedder 10 s (single query) · ES query 10 s · LLM 120 s · per-batch ingest embed 30 s (asymmetric — query is one string, ingest is up to 32) | `tests/integration/test_chat_endpoint.py` (T3.9), `tests/integration/test_chat_stream_endpoint.py` (T3.11), `tests/integration/test_chat_pipeline_retrieval.py` (T3.5) | **P1** sync |

### 4.4 Plugin Catalog

| Plugin | `name` | `required` | `queue` | `extract()` | `delete()` | Phase |
|---|---|:---:|---|---|---|:---:|
| `VectorExtractor`    | `vector`     | ✓ | `extract.vector` | embed `f"{source_title}\n\n{chunk_text}"` (B15) → ES bulk index by `chunk_id`, denormalising `title`, `source_app`, `source_workspace` onto each row (B15, B29) | ES bulk `_op_type=delete` | **P1** |
| `StubGraphExtractor` | `graph_stub` | — | `extract.graph`  | no-op | no-op | **P1** |
| `GraphExtractor`     | `graph`      | — | `extract.graph`  | LightRAG → Graph DB upsert | entity GC + ref_count | P3 |

### 4.5 Third-Party Client Catalog

| Client | Endpoint | Auth | Phase |
|---|---|---|:---:|
| `TokenManager`    | `AI_API_AUTH_URL/auth/api/accesstoken`          | J1 → J2 | **P1** |
| `EmbeddingClient` | `EMBEDDING_API_URL/text_embedding`              | J2 | **P1** |
| `LLMClient`       | `LLM_API_URL/gpt_oss_120b/v1/chat/completions` | J2 | **P1** |
| `RerankClient`    | `RERANK_API_URL/`                               | J2 | P1 unit / P2 wired |
| `HRClient`        | `HR_API_URL/v3/employees`                       | `Authorization` | P2 |

All 3rd-party calls: timeout/retry/backoff per `00_rule.md`; circuit-breaker on client.

**TokenManager refresh discipline (P-F):** the J1→J2 refresh path is **single-flight** — concurrent callers around the `expiresAt − 5 min` boundary share one in-flight refresh (in-process `asyncio.Lock` / `threading.Lock` keyed on the J1 token), avoiding stampede. The cached J2 is shared by Embedding/LLM/Rerank clients.

### 4.6 Environment Variables (C2 + B28)

> **Inventory rules (B28):** every external dependency, every per-call timeout, every operational threshold, and every credential MUST appear in this table. Code that reads a literal value not represented here is a spec drift bug. Vars marked `(required)` have no default and refuse boot.

#### 4.6.1 Bootstrap & HTTP server

| Variable | Default | Description |
|---|---|---|
| `RAGENT_ENV`                          | (required)       | `dev` \| `staging` \| `prod`. P1 startup guard refuses non-`dev`. |
| `RAGENT_AUTH_DISABLED`                | `false`          | Must be `true` in P1; P2 sets `false` to enable JWT (§3.5). |
| `RAGENT_HOST`                         | `127.0.0.1`      | API bind address. P1 OPEN guard (§1) refuses any value other than `127.0.0.1` while `RAGENT_ENV=dev` & `RAGENT_AUTH_DISABLED=true`. |
| `RAGENT_PORT`                         | `8000`           | API bind port. |
| `LOG_LEVEL`                           | `INFO`           | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. Applies to app + TaskIQ + Reconciler. |

#### 4.6.2 Datastore connections (boot-blocking)

| Variable | Default | Description |
|---|---|---|
| `MARIADB_DSN`                         | (required)       | Full SQLAlchemy DSN, e.g. `mysql+aiomysql://user:pass@host:3306/ragent?charset=utf8mb4`. Used by repositories, bootstrap, `/readyz`. |
| `ES_HOSTS`                            | (required)       | Comma-separated `https?://host:port` list. |
| `ES_USERNAME`                         | (optional)       | Basic-auth username; omit for unauthenticated dev clusters. |
| `ES_PASSWORD`                         | (optional)       | Basic-auth password. |
| `ES_API_KEY`                          | (optional)       | Alternative to user/password (mutually exclusive). |
| `ES_VERIFY_CERTS`                     | `true`           | Set `false` for self-signed dev clusters. |
| `MINIO_ENDPOINT`                      | (required)       | `host:port` (no scheme). |
| `MINIO_ACCESS_KEY`                    | (required)       | |
| `MINIO_SECRET_KEY`                    | (required)       | |
| `MINIO_SECURE`                        | `false`          | `true` ⇒ HTTPS. |
| `MINIO_BUCKET`                        | `ragent`         | Single staging bucket (B10). |

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
| `AI_API_AUTH_URL`                     | (required)       | TokenManager J1→J2 endpoint. |
| `AI_API_CLIENT_ID`                    | (required)       | J1 identity POSTed to `AI_API_AUTH_URL/auth/api/accesstoken`. **Never logged, never echoed in error responses.** |
| `AI_API_CLIENT_SECRET`                | (required)       | J1 secret. **Never logged, never echoed.** |
| `EMBEDDING_API_URL`                   | (required)       | bge-m3 endpoint. |
| `LLM_API_URL`                         | (required)       | gptoss-120b endpoint. |
| `RERANK_API_URL`                      | (required P2)    | Rerank endpoint (P1 unit-tests only; wired in P2). |
| `HR_API_URL`                          | (future)         | OpenFGA-related role lookup (P2+). |

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
| `INGEST_MAX_FILE_SIZE_BYTES`          | `52428800`       | 50 MB upload cap (B2); 413 on overrun. |
| `INGEST_LIST_MAX_LIMIT`               | `100`            | `GET /ingest?limit=` upper bound (§4.1, B7). |
| `CSV_CHUNK_TARGET_CHARS`              | `2000`           | `RowMerger` target merged-buffer size (B24). |
| `EMBEDDER_BATCH_SIZE`                 | `32`             | Chunks per embedder HTTP call (P-B). |
| `CHAT_JOIN_MODE`                      | `rrf`            | `rrf` \| `concatenate` \| `vector_only` \| `bm25_only` (C6). |
| `EXCERPT_MAX_CHARS`                   | `512`            | `SourceHydrator` truncation length (B23). |
| `RAGENT_DEFAULT_LLM_PROVIDER`         | `openai`         | Echoed when request omits `provider`. |
| `RAGENT_DEFAULT_LLM_MODEL`            | `gptoss-120b`    | Echoed when request omits `model`. |
| `RAGENT_DEFAULT_LLM_TEMPERATURE`      | `0.7`            | |
| `RAGENT_DEFAULT_LLM_MAX_TOKENS`       | `4096`           | |
| `RAGENT_DEFAULT_SYSTEM_PROMPT`        | `You are a helpful assistant` | Auto-prepended when `messages` lacks a `system` entry. |

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

> Timeouts above are intentionally asymmetric: ingest embedder uses 30 s/batch (32 strings), query embedder uses 10 s (1 string) (C8). Same client, two call sites, two budgets.

#### 4.6.8 Observability (OpenTelemetry)

| Variable | Default | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | (optional)       | OTLP collector URL; absence disables export (no-op tracer). |
| `OTEL_SERVICE_NAME`                   | `ragent-api`     | Per-process: `ragent-api` \| `ragent-worker` \| `ragent-reconciler`. |
| `OTEL_TRACES_SAMPLER`                 | `parentbased_traceidratio` | Standard OTEL SDK sampler name. |
| `OTEL_TRACES_SAMPLER_ARG`             | `0.1`            | Sampling ratio (10% by default; raise to `1.0` in dev). |

---

## 5. Data Structures

### 5.1 MariaDB

```sql
CREATE TABLE documents (
  document_id      CHAR(26)     PRIMARY KEY,
  create_user      VARCHAR(64)  NOT NULL,
  source_id        VARCHAR(128) NOT NULL,
  source_app       VARCHAR(64)  NOT NULL,
  source_title     VARCHAR(256) NOT NULL,
  source_workspace VARCHAR(64)  NULL,
  object_key       VARCHAR(256) NOT NULL,  -- MinIO key only (B10 format); bucket is config-driven (`MINIO_BUCKET`), not stored per-row (C3).
  status           ENUM('UPLOADED','PENDING','READY','FAILED','DELETING') NOT NULL,
  attempt          INT          NOT NULL DEFAULT 0,
  created_at       DATETIME(6)  NOT NULL,
  updated_at       DATETIME(6)  NOT NULL,
  INDEX idx_status_updated (status, updated_at),
  INDEX idx_source_app_id_status_created (source_app, source_id, status, created_at),
  INDEX idx_create_user_document (create_user, document_id)
);
-- (source_id, source_app) is the LOGICAL identity. Uniqueness is eventual,
-- enforced by the supersede task — not a DB UNIQUE constraint, since
-- transient duplicates are expected during ingestion.
-- create_user records WHO uploaded the row (audit / "my uploads" list).
-- It is NOT an authorization field — permission checks live in a
-- separate Permission Layer (see §3.5), not on this column.
-- idx_create_user_document supports "list documents I uploaded"
-- (`WHERE create_user=? AND document_id > ? ORDER BY document_id`).

CREATE TABLE chunks (
  chunk_id    CHAR(26)   PRIMARY KEY,
  document_id CHAR(26)   NOT NULL,
  ord         INT        NOT NULL,
  text        MEDIUMTEXT NOT NULL,
  lang        VARCHAR(8) NOT NULL,
  INDEX idx_document (document_id)
);
```

No physical FK. ORM-level cascade only.

**ID classification:**
- **Internal IDs** (UID rule applies — `00_rule.md` §ID Generation Strategy): `document_id`, `chunk_id` — `CHAR(26)` UUIDv7→Crockford Base32, generated by `new_id()`.
- **External IDs / display fields** (UID rule does **not** apply — supplied by clients or upstream systems): `source_id` (client-supplied stable identifier, any string ≤ 128 chars: URL hash, external doc ID, etc.), `source_app` (≤ 64 chars; namespace/source system, e.g. `confluence`, `slack`, `intranet`), `source_title` (≤ 256 chars; human-readable display title surfaced as `sources[].title` in chat), `source_workspace` (optional ≤ 64 chars; intra-app scope, e.g. team or space), `create_user` (≤ 64 chars; the `X-User-Id` of the request that created the row — audit metadata only, **not an authorization field**).
- The `task_id` returned from `POST /ingest` is the `document_id` itself; no separate task identifier exists.

### 5.2 Elasticsearch `chunks_v1`

> **Source of truth (B26):** `resources/es/chunks_v1.json` — settings + mappings, checked into git. Bootstrap (§6.1) reads this file and `PUT /chunks_v1` if the index does not exist. The block below is the canonical content; any drift between this spec snippet and the resource file is a CI failure (`tests/integration/test_es_resource_drift.py`).

```json
{
  "settings": {
    "analysis": {
      "analyzer": {
        "icu_text": {
          "type": "custom",
          "tokenizer": "icu_tokenizer",
          "filter": ["icu_folding", "lowercase"]
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "chunk_id":         { "type": "keyword" },
      "document_id":      { "type": "keyword" },
      "source_app":       { "type": "keyword" },
      "source_workspace": { "type": "keyword" },
      "lang":             { "type": "keyword" },
      "title":            { "type": "text", "analyzer": "icu_text" },
      "text":             { "type": "text", "analyzer": "icu_text" },
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

**BM25 analyzer (B26):** `icu_text` (custom analyzer) uses the `icu_tokenizer` from the `analysis-icu` plugin instead of the default `standard` analyzer. Rationale: `standard` does whitespace/punctuation splitting only — Chinese/Japanese/Korean text (no whitespace separators) becomes single mega-tokens or per-character tokens, both useless for BM25. `icu_tokenizer` segments CJK by Unicode word boundaries, giving meaningful BM25 hits for multilingual content. `icu_folding` normalises Unicode (full-width ⇄ half-width, accented ⇄ ascii); `lowercase` normalises Latin script. The same analyzer covers `text` and `title` fields — consistent with `multi_match` ranking (B15).

**Vector index (B26):** `embedding` uses `index_options.type = bbq_hnsw` (Better Binary Quantization HNSW, ES 8.16+). Quantises 1024-dim float vectors to 1-bit per dimension (~32× memory reduction) with negligible recall loss for `cosine` similarity at our scale. Falls back to standard HNSW if `bbq_hnsw` rejected by the cluster (e.g. dev cluster on older ES) — log `event=es.bbq_unsupported` and continue.

**Plugin requirement:** the `analysis-icu` plugin MUST be installed on every ES node (dev / CI / prod). Boot-time `/readyz` probe (B4) refuses ready if the plugin is not loaded.

**Title surface (B15):** `title` is denormalised onto every chunk row from `documents.source_title`. Two retrieval surfaces are derived from it:
1. **Lexical** — BM25 retriever runs `multi_match` on `["text", "title^2"]` (title boosted 2× over body) using the `icu_text` analyzer (B26).
2. **Semantic** — `embedding` is computed as `embed(f"{source_title}\n\n{chunk_text}")` at ingest time, so every chunk vector already carries title semantics. No separate `title_embedding` field is stored.

**Filter surface (B29):** `source_app` and `source_workspace` are denormalised from `documents` onto every chunk row as `keyword` fields. Chat (§3.4.1) accepts optional `source_app` and `source_workspace` filter params; when present they apply as ES `term` filters in **both** retrievers' `filter` clause (kNN `filter` and BM25 `bool.filter`). Filtering happens **before** scoring narrows the candidate pool, so top-K returned reflects the requested scope without over-fetch. These are **not auth fields** (B14): they are content-scope metadata, like `lang`. Permission gating remains a separate post-retrieval layer (§3.5).

### 5.3 ID / DateTime

- `new_id()` → UUIDv7 → Crockford Base32 → 26 chars (lexicographically sortable).
- `utcnow()` → tz-aware UTC. `to_iso()` → ISO 8601 `...Z`. `from_db(naive)` → attach UTC.

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

**Boot-time auto-init (idempotent):**
- On startup, the bootstrap module runs `CREATE TABLE IF NOT EXISTS … / CREATE INDEX IF NOT EXISTS …` against MariaDB derived from `migrations/schema.sql`, and `PUT /<index>` for ES if the index does not exist — **using the JSON body in `resources/es/<index>.json`** (e.g. `resources/es/chunks_v1.json`, B26). Existing tables/indexes are left untouched.
- Resource files are the single source of truth for ES index definitions; spec §5.2 mirrors them in prose. `tests/integration/test_es_resource_drift.py` parses both and rejects drift.
- Auto-init is for dev/test bring-up convenience only; production migrations MUST go through Alembic (DB) or a controlled `PUT /<index>-vN` + reindex flow (ES). Boot-init refuses to run any `ALTER` or ES mapping update — schema drift is logged as `event=schema.drift` and surfaces in `/readyz` as a degraded state, not an automatic mutation.

**Invariant:** `schema.sql` ≡ replaying `001 → NNN`. CI enforces this with `tests/integration/test_schema_drift.py` (apply both paths to two scratch DBs, `mysqldump` both, diff must be empty).

---

## 7. Decision Log

> Frozen 2026-05-04. Each row records a once-blocking design choice with the alternatives considered. Changes require a new dated row (append-only, never edit in place).

| ID | Date | Domain | Question | Decision | Alternatives rejected | Affects |
|---|---|---|---|---|---|---|
| **B1** | 2026-05-04 | NLP | Chinese chunking strategy in `LanguageRouter` | **Sentence-level split** with `en_splitter` (default) and `cjk_splitter` (CJK branch). Both emit one chunk per sentence; downstream embedder batches them (32/call). | jieba word-segmentation (heavyweight, P3 graph concern); omit CN in P1 (kills demo). | §3.2 / §4.3 / T3.1 |
| **B2** | 2026-05-04 | Format | CSV row scaling (10⁵-row file → 10⁵ chunks?) | **No row cap.** Inherit the global 50 MB file-size limit (already in §4.2). Operator concern, not pipeline concern. | Per-file row cap (arbitrary); group N rows/chunk (loses row identity); omit `.csv` in P1. | §4.2 / T3.1 |
| **B3** | 2026-05-04 | DB | Migration tool | **Both:** `migrations/schema.sql` (consolidated snapshot, kept current) + `migrations/NNN_*.sql` (Alembic-applied incrementals). Boot performs idempotent `CREATE … IF NOT EXISTS` for MariaDB tables/indexes and ES indexes; never `ALTER`. | Alembic-only (no quick CI bring-up); raw-only (no audit trail of changes); sqlx-style (Python toolchain mismatch). | §6.1 / T0.8 |
| **B4** | 2026-05-04 | Ops | Health/metrics endpoints | **App layer:** `/livez`, `/readyz`, `/metrics`. K8s probes use `/livez` for liveness and `/readyz` for readiness; Prometheus scrapes `/metrics`. **Infra layer:** K8s pod-level liveness only (no in-app dep probes for liveness — would cause cascading restarts on transient ES blips). | Single `/health` endpoint (conflates liveness vs readiness); separate sidecar exporter (extra deploy unit). | §4.1 / T7.1 / T7.7 |
| **B5** | 2026-05-04 | API | REST error response shape | **RFC 9457 Problem Details** (`application/problem+json`) with extension `error_code` (stable `SCREAMING_SNAKE_CASE` business identifier). 422 also carries `errors[]` for field validation. | Bare `{error, message}` (no standard, no machine-readable code); RFC 7807 (superseded by 9457). | §4.1.1 / T2.13 |
| **B6** | 2026-05-04 | API/SSE | Mid-stream error contract on `/chat` | **`data:` line with payload `{type:"error", error_code, message}`**, then close. No `event: error` named-event — keeps client parser uniform (every line is JSON). Pre-stream errors use normal RFC 9457 response. | `event: error` named SSE event (forces dual parser path); silently truncate (loses error_code). | §3.4 / T3.3 |
| **B7** | 2026-05-04 | API | `GET /ingest?after=&limit=` semantics | **Cursor pagination by `document_id` ASC** (UUIDv7 → time-ordered). `after` = last `document_id` of previous page; server returns `next_cursor` = last id of current page. Future "my uploads" view adds `WHERE create_user=?` predicate using `idx_create_user_document` (B14). | OFFSET-based (linear scan); page-number based (incompatible with cursor stability); keyset on `created_at` (collisions). | §4.1 / T2.11 |
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
| **B18** | 2026-05-04 | Resilience | Per-document pipeline timeout | **Hard ceiling `PIPELINE_TIMEOUT_SECONDS` (default 1800 = 30 min)** around the worker pipeline body. Overrun ⇒ `FAILED` with `error_code=PIPELINE_TIMEOUT`, full cleanup. Bounds pathological inputs (huge CSV, runaway plugin) deterministically; heartbeat catches faster (5 min) but timeout is the deterministic upper bound. | No ceiling (relies on heartbeat alone; allows worker pods to be tied up indefinitely on bad data); reject docs at upload time by estimated processing cost (estimation is unreliable). | §3.1 / §3.2 / §4.6 / S34 |
| **B19** | 2026-05-04 | Doc hygiene | §4.3 Pipeline Catalog Chat row was stale post-B12 (single endpoint, no SourceHydrator, no join-mode toggle) | Updated row to reference `LLMClient.{chat\|stream}`, `SourceHydrator`, `CHAT_JOIN_MODE` (C6), and the three integration test paths (T3.5 / T3.9 / T3.11). Also added the asymmetric-timeout note (C8). | Leave stale (causes plan/spec drift; future readers wire the wrong test). | §4.3 |
| **B20** | 2026-05-04 | BDD hygiene | S4 / S5 / S9 / S11 referenced but no Given/When/Then bodies | Inlined full bodies in §3.3 (S4, S5, S11) and §3.5 (S9). | Footnote-style "see plan" (BDD scenarios live in spec, not plan, per project convention). | §3.3 / §3.5 |
| **B21** | 2026-05-04 | API | JSON field naming convention | **IDs / resources = `snake_case`** (`document_id`, `source_id`, `error_code`, `next_cursor`, …); **LLM token/config knobs = `camelCase`** (`maxTokens`, `promptTokens`, `completionTokens`, `totalTokens`, `temperature`). Mixed within one body is allowed; the rule above resolves which side a new field falls on. Preserves OpenAI-shape upstream familiarity for chat tokens while keeping ingest/data fields snake-case. | All-snake (breaks user-specified chat shape); all-camel (forces `documentId`/`sourceId` rename across ingest, schema, OpenFGA tuples, audit logs); ad-hoc per field (was the bug). | §6 / all body schemas |
| **B22** | 2026-05-04 | Chat API | `provider` field semantics in P1 | **Validated allow-list `{"openai"}`**, 422 (`error_code=CHAT_PROVIDER_UNSUPPORTED`) on others; the accepted value is **echoed verbatim** in the response. P1 routes nothing on it. Future phases extend the allow-list and use `provider` as a routing key. | Echo only, no validation (silently accepts garbage); ignore the field entirely (forward-incompat with multi-provider future). | §3.4.1 / §3.4.2 / T3.3–T3.4 |
| **B23** | 2026-05-04 | Chat API | Where `sources[].excerpt` is truncated | **Inside `SourceHydrator`**, single locus, `EXCERPT_MAX_CHARS` (default 512), append `…` if cut. Router never re-truncates. | Truncate in router (then chunks-as-LLM-context still see the long form, leak); truncate in retriever (couples retrieval to display concerns); leave to client (LLM gets full chunk for context, response surfaces full chunk to client — wasted bandwidth + risk of leaking text the operator intended to keep server-side). | §3.4.2 / T3.6 |
| **B24** | 2026-05-04 | Pipeline | CSV row scaling — naive 1 row → 1 chunk would be 1 M chunks for a 50 MB file | **`RowMerger`** SuperComponent on the CSV branch only (Haystack `ConditionalRouter` on MIME): pack consecutive rows joined by `\n` until buffered text reaches `CSV_CHUNK_TARGET_CHARS` (default 2 000 ≈ 512 tokens, well under bge-m3's 8 192). Result: 50 MB CSV → ~25 k chunks instead of ~1 M. Combined with B18 ceiling, large CSVs are bounded both in count and total wall-clock. | Per-file row cap (loses tail rows); skip CSV in P1 (regression vs spec); change every row's chunker to "passage" (loses sentence-level fidelity for non-CSV); dynamic per-row Haystack `DocumentSplitter(split_by="word")` (no row-grouping, defeats the "one logical record per chunk" property). | §3.2 / §4.2 / S35 |
| **B25** | 2026-05-04 | Storage | `documents.storage_uri` stored full URI; bucket name is constant config | **Rename column to `object_key VARCHAR(256) NOT NULL`** (key only, format per B10). Bucket is read from `MINIO_BUCKET` env var (default `ragent`); reconstruct full URI on demand. Saves ~20 bytes/row, decouples row from bucket-rename ops, and makes a future bucket migration a config flip. | Keep full URI (rigid); store bucket per-row (rotation hell); URL-encode in object key (key+bucket separation already does the job). | §5.1 / T2.5 / T2.6 |
| **B26** | 2026-05-04 | ES | (a) BM25 analyzer; (b) vector index type; (c) where the index definition lives | **(a) `icu_text` custom analyzer** (`icu_tokenizer` + `icu_folding` + `lowercase`) on `text` and `title` — required for CJK tokenisation; `standard` analyzer collapses CJK to per-character or mega-tokens, breaking BM25. `analysis-icu` plugin is a hard ES dependency (verified at `/readyz`). **(b) `bbq_hnsw`** (Better Binary Quantization HNSW, ES 8.16+) on `embedding` — ~32× memory reduction at negligible recall cost; falls back to standard HNSW with `event=es.bbq_unsupported` log if cluster rejects. **(c) Source of truth = `resources/es/chunks_v1.json`** loaded by boot auto-init (§6.1) when the index does not exist; spec §5.2 mirrors the file in prose; CI drift test enforces equality. | Default `standard` analyzer (CJK becomes useless for BM25); `nori`/`smartcn` (per-language plugin sprawl, doesn't cover all CJK consistently); raw HNSW (4× more memory at our 1024 dims); inline mapping in Python code (every change is a code commit, no resource-file diffability). | §5.2 / §6.1 / T0.8d / T0.9 |
| **B27** | 2026-05-04 | Infra | Redis topology — single-instance vs Sentinel HA | **Per-instance toggle via `REDIS_MODE` env (`standalone` \| `sentinel`)**. Both broker and rate-limiter share the mode; standalone reads `REDIS_BROKER_URL` / `REDIS_RATELIMIT_URL`; sentinel reads `REDIS_SENTINEL_HOSTS` (shared quorum) + `REDIS_*_SENTINEL_MASTER` (per-instance master name). Connection layer dispatches on mode (`redis-py-sentinel` vs `redis-py`). Default `standalone` for dev/CI; prod sets `sentinel`. | Hardcode Sentinel (broken local dev); hardcode standalone (no prod HA story); per-instance independent mode (config matrix doubles, no real-world need). | §3.6 / §4.6 / T0.9 |
| **B30** | 2026-05-04 | Operator UX | What does an operator have to do to bring up the system end-to-end? | **Two-command quickstart**: `cp .env.example .env` → fill required vars → `python -m ragent.api` (T7.5d) and `python -m ragent.worker` (T7.5e). All else is automatic: schema/index auto-init runs from FastAPI lifespan + worker startup (T0.8d, idempotent); composition root (T7.5a) wires every dependency from env vars, no per-module env reads; TaskIQ broker module (T0.10) is the single import point for `@broker.task` decorators; `.env.example` (T0.11) is symmetric with spec §4.6 (drift test T0.11a). Reconciler is K8s-only and not required for the local two-command path (recovery surface, not steady-state). E2E quickstart asserted by T7.2 launching the real entrypoint subprocesses, not internal scaffolding. | (a) Manual `alembic upgrade head` step before boot — adds an operator-facing migration command, defeats "two commands"; (b) per-module env reads — couples every module to env, blocks DI testing, rule J28 violation; (c) split broker module per task — multiple import paths, decorator misregistration risk; (d) no `.env.example` — operator reads spec §4.6 by hand, easy to miss required vars and discover at first failed request. | §1 / §3.1 / §6.1 / §4.6 / T0.10 / T0.11 / T7.5 / T7.5a–f / T7.2 |
| **B29** | 2026-05-04 | Chat API | Optional retrieval filter by `source_app` / `source_workspace` | **Filter in ES via denormalised keyword fields.** `chunks_v1` mapping gains two `keyword` fields (`source_app`, `source_workspace`) populated by `VectorExtractor` from `documents` at ingest. Chat request schema (§3.4.1) accepts both as optional fields; when present they apply as ES `term` filter in **both** retrievers' `filter` clause (kNN `filter`, BM25 `bool.filter`). AND semantics when both supplied. Empty string ⇒ 422 `CHAT_FILTER_INVALID`. These are scope metadata, not auth fields (B14 distinction preserved); permission gating remains a separate post-retrieval layer (§3.5). | Post-retrieval filter via document JOIN in SourceHydrator (forces over-fetch with unbounded `K' = K × overfetch_factor` — narrow workspaces silently truncate); filter on `documents` only, retrieve all chunks then drop (defeats kNN top-K semantics); add a third retriever per filter combination (mapping bloat, no win). Pre-existing `chunks_v1` data does not exist (still pre-implementation), so single-version mapping update is safe; would otherwise require `chunks_v2` + reindex. | §3.4 / §3.4.1 / §4.3 / §4.4 / §5.2 / `resources/es/chunks_v1.json` / T1.9 / T1.12 / T3.5 |
| **B28** | 2026-05-04 | Config | Env-var inventory was incomplete — missing datastore connections (MariaDB/ES/MinIO host/creds), J1 client credentials, HTTP bind, OTEL exporter, retry/timeout policy knobs, upload limits, and log level; `RERANK_API_URL` was misspelled `REREANK_API_URL` | **Reorganise §4.6 into 8 subsections** (bootstrap, datastore, redis, third-party clients, worker/reconciler, pipeline/chat, per-call timeouts, observability). **Add 26 new vars** covering every previously implicit literal: `MARIADB_DSN`, `ES_HOSTS`/`ES_USERNAME`/`ES_PASSWORD`/`ES_API_KEY`/`ES_VERIFY_CERTS`, `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_SECURE`, `RAGENT_HOST`/`RAGENT_PORT`/`LOG_LEVEL`, `AI_API_CLIENT_ID`/`AI_API_CLIENT_SECRET`, `WORKER_MAX_ATTEMPTS`, `RECONCILER_PENDING_STALE_SECONDS`/`RECONCILER_UPLOADED_STALE_SECONDS`/`RECONCILER_DELETING_STALE_SECONDS`, `INGEST_MAX_FILE_SIZE_BYTES`, `INGEST_LIST_MAX_LIMIT`, the seven per-call timeouts (`EMBEDDER_INGEST/QUERY`, `ES_BULK/QUERY`, `MINIO_GET/PUT`, `LLM`, `PLUGIN_FAN_OUT`, `READYZ_PROBE`), plus four `OTEL_*` vars. **Fix typo** `REREANK_API_URL` → `RERANK_API_URL`. **Rename** ambiguous `RECONCILER_STALE_AFTER_SECONDS` to per-state `RECONCILER_PENDING_STALE_SECONDS` and add UPLOADED/DELETING siblings. **Change `MINIO_BUCKET` default** from `ragent-staging` → `ragent` (B10/B25 prose updated). Also adds an inventory rule: any literal value read by code that is not represented in §4.6 is a spec drift bug. | Leave datastore connections as "implicit per-environment overrides" (every operator reinvents the wheel; bootstrap module has no canonical names to read); expose only DSN-style strings for ES/MinIO too (forces credential concatenation in URLs, harder to rotate); keep timeouts as code constants only (violates J21 rule "every call site lists per-call timeout AND aggregate ceiling"); ship without J1 creds (TokenManager has a URL but no way to authenticate — boot succeeds but every embedder/LLM call fails on first request). | §1 / §3.1 / §3.6 / §3.7 / §4.5 / §4.6 / §6.1 / T0.8 |
