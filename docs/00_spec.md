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

> **v2 OVERRIDE (2026-05-06)** — see `docs/team/2026_05_06_ingest_api_v2.md` for the full team decision; the operative contract below supersedes the v1 multipart description further down.
>
> **API:** `POST /ingest` is **JSON only** (no multipart). Body discriminator `ingest_type ∈ {inline, file}`:
> - `inline` → `{ingest_type, mime_type, content: str, source_id, source_app, source_title, source_meta?, source_url?}`. `content` is UTF-8; size ceiling `INGEST_INLINE_MAX_BYTES` (default 10 MB) on the encoded byte length. API stages the content to MinIO `__default__` site.
> - `file` → `{ingest_type, mime_type, minio_site, object_key, source_id, source_app, source_title, source_meta?, source_url?}`. API HEAD-probes `(minio_site, object_key)`; absent → 422 `INGEST_OBJECT_NOT_FOUND`; size > `INGEST_FILE_MAX_BYTES` (50 MB) → 413. **No copy** — worker reads from caller's bucket.
>
> **MIME allow-list:** `text/plain`, `text/markdown`, `text/html`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (DOCX), `application/vnd.openxmlformats-officedocument.presentationml.presentation` (PPTX), `application/pdf` (PDF). Anything else → 415 `INGEST_MIME_UNSUPPORTED`. **CSV is dropped** (was v1).
>
> **Source columns:** `source_id`, `source_app`, `source_title`, `source_meta?` (free-format ≤ 1024 chars; renamed from `source_workspace` per B35) **plus new `source_url`** (opaque ≤ 2048 chars, display-only in citations).
>
> **MinIO multi-site:** server reads `MINIO_SITES` JSON env at boot → `MinioSiteRegistry`. `__default__` is mandatory (used by inline). Sites with `read_only=true` refuse post-READY delete (`file` ingests against caller-owned buckets).
>
> **Cleanup branching by `documents.ingest_type`:**
> | `ingest_type` | Worker reads from | Post-READY delete |
> |---|---|---|
> | `inline` | `__default__/<object_key=document_id>` | yes (same as v1) |
> | `file` | caller's `(minio_site, object_key)` | **no** (object isn't ours) |
>
> **Storage model (revised):** chunks live **only** in ES `chunks_v1`. The MariaDB `chunks` table is **dropped**. `documents` keeps metadata. Two stores total: `documents` (MariaDB, metadata) + `chunks_v1` (ES, content + embedding + raw_content).
>
> **Per-step structured logging (Logging Rule extension):** every pipeline component emits `event=ingest.step.{started,ok,failed}` with `{document_id, step, mime_type, duration_ms, error_code?, error?}`. Failures map to a small enum (`PIPELINE_UNROUTABLE`, `EMBEDDER_ERROR`, `ES_WRITE_ERROR`, `PIPELINE_TIMEOUT`). Happy path emits `event=ingest.ready` with chunk count. This is how operators answer "which doc, which step, why failed" from logs alone.
>
> **State machine, locking, heartbeat, supersede, reconciler arms — unchanged from v1.** Only the ingress and pipeline interior change.

**State machine:** `UPLOADED → PENDING → READY | FAILED`; `DELETING` transient on delete.

**Locking discipline:**
- Status mutations use **atomic conditional UPDATE** with rowcount-based dispatch — no `SELECT FOR UPDATE [NOWAIT]` on the documents row-mutation paths (`claim_for_processing`, `claim_for_deletion`, `update_status`, `promote_to_ready_and_demote_siblings`). The canonical idiom is `UPDATE documents SET status=:to_status, … WHERE document_id=:id AND status IN (:accept_set)`; `rowcount=1` means the caller won the transition, `rowcount=0` means another writer beat us (or the row is gone) and the caller no-ops gracefully. InnoDB's row-level X-lock during the UPDATE statement serialises concurrent writers — the lock window is microseconds, not the multi-statement span of the old `SELECT FOR UPDATE` pattern. (The reconciler stale-sweep and supersede single-loser-per-tx scan retain `SKIP LOCKED` semantics — those are bulk load-shedding scans across rows, not single-row transitions.)
- Pipeline body runs **outside any DB transaction**: no row locks are held while external calls (embedder, ES, plugins, MinIO) run. The worker's claim (TX-A) is a single atomic UPDATE that flips `UPLOADED|PENDING → PENDING` and bumps `attempt`; the terminal commit (TX-B) is another single atomic UPDATE that flips `PENDING → READY|FAILED`. Heartbeat keeps the `PENDING` row warm; the Reconciler's stale-sweep is governed entirely by `updated_at`, not by row locks.
- `update_status` and the claim path both validate transitions via the WHERE clause's accept-set; an invalid transition surfaces as `rowcount=0` (no exception) on the claim path, and as `IllegalStateTransition` (raised in code on rowcount=0) on the `update_status` path — preserving the v1 error contract for terminal writes.

**Worker heartbeat (B16) — closes the no-lock-window race:** because the pipeline body holds no row lock, a naive Reconciler "PENDING > 5 min" sweep would happily re-dispatch a still-running worker and produce double processing. The worker therefore **updates `documents.updated_at = NOW()` every 30 s** during the pipeline body (background timer; one cheap PK-keyed `UPDATE`). The Reconciler's threshold becomes `WHERE status='PENDING' AND updated_at < NOW() - INTERVAL 5 MINUTE` — only **stale-heartbeat** rows are re-dispatched. Heartbeat interval is configured via `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30).

**Per-document pipeline timeout (B18):** the worker enforces an overall ceiling of `PIPELINE_TIMEOUT_SECONDS` (default 1800 s = 30 min) around the pipeline body. On overrun, the worker transitions the row to `FAILED` with `error_code=PIPELINE_TIMEOUT` and runs the §3.1 R5 cleanup path (`fan_out_delete` + `delete_by_document_id`). This bounds pathological-document worst case so the Reconciler never sees an infinitely-running worker (heartbeat would also catch it after 5 min, but the ceiling makes the failure deterministic).

**Storage model:** MinIO is **transient staging only** — needed because Router (API) and Worker may run on different hosts. The original file is deleted from MinIO after the pipeline reaches a terminal state (`READY` or `FAILED`). After ingest, only chunks (ES) + metadata (MariaDB) remain.

**Object key convention (B10):** `{source_app}_{source_id}_{document_id}` (single bucket from `MINIO_BUCKET` env, default `ragent`). `source_app` and `source_id` are sanitized to `[A-Za-z0-9._-]` (other chars percent-encoded) to satisfy MinIO key constraints. The `document_id` suffix guarantees uniqueness even when the same `(source_app, source_id)` is re-POSTed before supersede converges.

**Pipeline retry idempotency:** Every pipeline run begins with `ChunkRepository.delete_by_document_id(document_id)` and `VectorExtractor.delete(document_id)` (idempotent ES bulk-delete) so a Reconciler retry of a partially-written attempt does not produce duplicate chunks. `chunk_id` may therefore be a fresh `new_id()` per run; identity is by `(document_id, ord)`.

**Supersede model (smart upsert):** Every `POST /ingest` carries a mandatory `(source_id, source_app, source_title)` triple (e.g. `("DOC-123", "confluence", "Q3 OKR Planning")`) and an optional `source_meta` (free-format ≤ 1024 chars). The `(source_id, source_app)` pair is the **logical identity** of a document; `source_title` is human-readable display text required by chat retrieval (`sources[].title` in §3.4). At steady state at most one `READY` row may exist per `(source_id, source_app)`. A new POST always creates a fresh `document_id`; when it reaches `READY`, the system enqueues a **supersede** task that selects every `READY` row sharing the same `(source_id, source_app)`, keeps the one with `MAX(created_at)`, and cascade-deletes the rest. This guarantees "latest write wins" even when documents finish out-of-order, gives zero-downtime replacement (old chunks remain queryable until the new ones are indexed), and preserves the old version if the new ingest fails. Supersede is enqueued **only** on the `PENDING → READY` transition; FAILED or mid-flight DELETE never triggers it. Uniqueness is **eventual**, enforced by supersede — not by a DB UNIQUE constraint, since transient duplicates are expected during ingestion. **Mutation = re-POST with the same `(source_id, source_app)` (and updated `source_title` if the title changed); there is no PUT/PATCH endpoint.**

**Create flow:**
1. `POST /ingest` (`source_id`, `source_app`, `source_title`, optional `source_meta`) → MIME/size validation → MinIO upload (staging) → `documents(UPLOADED, source_id, source_app, source_title, source_meta)` → kiq `ingest.pipeline` → 202.
2. Worker `ingest.pipeline`:
   - **TX-A (atomic claim):** single statement `UPDATE documents SET status='PENDING', attempt=attempt+1, updated_at=NOW(6) WHERE document_id=:id AND status IN ('UPLOADED','PENDING')`. `rowcount=1` → proceed; `rowcount=0` → log `event=ingest.claim_skipped` and exit gracefully (row is already READY/FAILED/DELETING or missing). The `PENDING` source state in the accept-set is what makes reconciler redispatch and manual rerun (§3.1.x) safe.
   - **Heartbeat (B16):** start a background timer that issues `UPDATE documents SET updated_at=NOW() WHERE document_id=?` every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30) for the lifetime of the pipeline body. Cancelled in `finally` before TX-B.
   - **Pipeline body (no DB tx, wall-clock-bounded by `PIPELINE_TIMEOUT_SECONDS`, default 1800):** `delete_by_document_id` (idempotency) → run §3.2 → `fan_out` → all required ok ⇒ outcome=`READY`; any required error ⇒ outcome=`PENDING_RETRY` (no terminal commit; Reconciler resumes); attempt > 5 ⇒ outcome=`FAILED`; ceiling exceeded ⇒ outcome=`FAILED` with `error_code=PIPELINE_TIMEOUT` (B18).
   - **TX-B (terminal only):** commit `READY` or `FAILED`. **On `FAILED`, also call `fan_out_delete` + `delete_by_document_id` to clean partial output before commit.**
   - **Post-commit (best-effort, no tx):** `MinIOClient.delete_object` (errors swallowed → log `event=minio.orphan_object`); on `READY`, kiq `ingest.supersede(document_id)` (idempotent).
3. Worker `ingest.supersede`:
   - **Single-loser-per-tx:** in a loop, `SELECT 1 row` matching `(source_id, source_app, status='READY')` ordered ASC by `created_at` (i.e. the oldest non-survivor) using `FOR UPDATE SKIP LOCKED` → cascade-delete that row → commit → repeat. Survivor = whichever row remains last; query naturally stops when only one `READY` row is left for the pair. **Avoids holding K row-locks across K cascades.**
   - Naturally idempotent: re-delivery finds ≤ 1 row and no-ops.

**Delete flow:**
1. `DELETE /ingest/{id}` → atomic claim `UPDATE … SET status='DELETING' WHERE document_id=:id AND status IN ('UPLOADED','PENDING','READY','FAILED')`. `rowcount=1` → outside-tx cascade (`fan_out_delete` → `delete_by_document_id` → if prior status was `PENDING/UPLOADED` also `MinIO.delete_object` → final tx: delete row → 204). `rowcount=0` → silent 204 (idempotent re-DELETE or row already DELETING).
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
- **S24 UPLOADED orphan recovered (R1)** — Given a row in `UPLOADED` for > 5 min (TaskIQ message lost or broker outage at POST time), When the Reconciler runs, Then it re-kiqs `ingest.pipeline` and the doc proceeds normally.
- **S25 pipeline retry produces no duplicate chunks (R4)** — Given a Reconciler retry of a previously partially-written ingest, When the pipeline reruns, Then `chunks` count for that `document_id` equals the chunker's output and `chunks_v1` ES index has no orphans.
- **S26 multi-READY invariant repaired (R3)** — Given two `READY` rows for the same `(source_id, source_app)` (e.g. supersede task lost between status commit and kiq), When the Reconciler runs, Then it re-enqueues `ingest.supersede` and convergence happens within one cycle.
- **S27 FAILED transition cleans partial output (R5)** — Given a doc transitions to `FAILED`, When the FAILED state is committed, Then `chunks` and ES `chunks_v1` for that `document_id` have been cleared (no leakage into chat retrieval).
- **S28 worker claim race (R7)** — Given two workers receive the same `document_id` (initial kiq + Reconciler dispatch), When both run the atomic-claim UPDATE concurrently, Then InnoDB serialises them on the row's brief X-lock: the first transitions `UPLOADED→PENDING` (or refreshes `PENDING→PENDING`) and proceeds; the second's UPDATE may also succeed (status was still `PENDING`) — pipeline idempotency (`delete_by_document_id` first step) keeps chunks consistent — or returns `rowcount=0` if the first already advanced the row past the accept-set, in which case the loser logs `event=ingest.claim_skipped` and exits. Neither path raises `LockNotAvailable`; the legacy `NOWAIT`-based contention story no longer applies.
- **S30 reconciler heartbeat (R8)** — Given a Reconciler tick runs, Then `reconciler_tick_total` increments and `event=reconciler.tick` is emitted; absence > 10 min triggers Prometheus alert.
- **S31 supersede single-loser-per-tx (P-C)** — Given supersede must delete K=10 losers, When the task runs, Then each is deleted in its own committed tx (loop), not one tx holding K row-locks.
- **S33 worker heartbeat (B16)** — Given a worker runs 4 min, When the Reconciler ticks at 5 min, Then it sees `updated_at` refreshed < 30 s ago and does **not** re-dispatch. When a worker dies and `updated_at` ages past 5 min, Reconciler re-dispatches exactly once.
- **S34 pipeline timeout (B18)** — Given a pipeline body runs longer than `PIPELINE_TIMEOUT_SECONDS`, When the ceiling fires, Then the worker transitions the row to `FAILED` with `error_code=PIPELINE_TIMEOUT`, runs cleanup (`fan_out_delete` + `delete_by_document_id`), and emits `event=ingest.failed reason=pipeline_timeout`.
- **S41 manual rerun** — Given a document with status in `{UPLOADED, PENDING, FAILED}`, When `POST /ingest/v1/{id}/rerun` is called with `X-User-Id`, Then the row's `status` is flipped to `PENDING`, `attempt` is reset to `0` (so an exhausted FAILED row isn't immediately re-FAILED by the reconciler's `_mark_failed` budget check), `error_code`/`error_reason` are cleared, `ingest.pipeline` is re-enqueued, and the response is `202 {"document_id": ...}`. Given the status is `READY` or `DELETING`, the response is `409 INGEST_NOT_RERUNNABLE` (use re-POST with same `(source_id, source_app)` for READY supersede). Given the document does not exist, the response is `404 INGEST_NOT_FOUND`.

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

**Performance & timeout discipline:**
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
QueryEmbedder → {ESVectorRetriever (kNN on `embedding`, optional filter)
                 ∥ ESBM25Retriever (multi_match on `["text", "title^2"]`, optional filter)
                 ∥ FeedbackMemoryRetriever (kNN on `feedback_v1.query_embedding`, optional;
                                            present iff CHAT_FEEDBACK_ENABLED + CHAT_JOIN_MODE=rrf, B50)}
              → DocumentJoiner(RRF, weights=[1, 1, CHAT_FEEDBACK_RRF_WEIGHT])
              → SourceHydrator(JOIN documents)
              → LLMClient.{chat | stream}
```

Title participates in **both** retrieval surfaces (B15): semantic (baked into every chunk's `embedding` at ingest via `embed(f"{title}\n\n{text}")`) and lexical (BM25 boosted 2× via `multi_match`). No separate title-only retriever, no extra ES field beyond `title`.

**SourceHydrator gate (B36):** Chunks whose `document_id` has no `READY` row in `documents` are **dropped** (not passed through with empty fields). Orphan chunks, in-flight rows, `DELETING` rows never reach LLM or `sources[]`. Reconciler = disk reclaim, not retrieval correctness.

**Filter scope (B29 → B35):** Optional `source_app` / `source_meta` request params (§3.4.1) translate to ES `term` filters applied to both retrievers' `filter` clause. Both fields denormalised onto chunks at ingest (§5.2). Empty filter ⇒ unrestricted retrieval (current P1 behaviour). Both filters AND together when both are supplied.

**Two endpoints (B12):** `POST /chat` (sync JSON) and `POST /chat/stream` (SSE: `delta` events → terminal `done` event carrying the same §3.4.2 body). Same request schema; only the LLM call differs.

**Join mode (`CHAT_JOIN_MODE`):** `rrf` (default, RRF k=60) | `concatenate` | `vector_only` | `bm25_only`. Factory assembles graph at startup; chat router is mode-agnostic.

**P1 OPEN:** no permission gating. ES queries permission-blind in every phase; Permission Layer post-filters in P2+ (§3.5). P1 retrievers run sequentially; P2 makes them concurrent (AsyncPipeline).

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

- `messages` required; all other fields optional (fall back to defaults above).
- `source_app` (≤ 64) / `source_meta` (≤ 1024): optional ES `term` filters (AND when both); empty string → 422 `CHAT_FILTER_INVALID`; omit to skip (B29→B35).
- `top_k` (default `RETRIEVAL_TOP_K`, default 20, range 1–200): max chunks to LLM context.
- `min_score` (default `RETRIEVAL_MIN_SCORE`, default `null`): score floor; `null` = no filtering.
- `maxTokens` caps LLM output; `provider` validated against `{"openai"}` allow-list (B22), echoed verbatim, 422 `CHAT_PROVIDER_UNSUPPORTED` otherwise.
- Missing `role:"system"` → server prepends default. Retrieval query = last `role:"user"` message.

#### 3.4.2 Response schema

`/chat` (non-streaming, `Content-Type: application/json`) and the terminal `done` event of `/chat/stream` both carry:

```json
{
  "content":        "COMPLETE_MARKDOWN_RESPONSE",
  "usage":          {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
  "model":          "gptoss-120b",
  "provider":       "openai",
  "sources":        [
    {
      "document_id":  "01J9...",
      "source_app":   "confluence",
      "source_id":    "DOC-123",
      "source_meta":  "engineering",
      "type":         "knowledge",
      "source_title": "Q3 OKR Planning",
      "source_url":   "https://wiki.example/q3-okr",
      "mime_type":    "text/markdown",
      "excerpt":      "...chunk text snippet...",
      "score":        0.87
    }
  ],
  "request_id":     "01J9...",
  "feedback_token": "<base64url>.<hmac_hex>"
}
```

- `sources` is `null` when empty; otherwise all fields are populated. `type` is always `"knowledge"` in P1 (reserved enum).
- `sources[].source_title/url/mime_type` from `documents`; `score` is RRF retrieval score; `excerpt` is truncated to `EXCERPT_MAX_CHARS` (default 512) in router (B23) — LLM receives full text untruncated.
- `usage` from `LLMClient` (non-streaming only; streaming `done` event omits `usage` — P1 limitation).
- `request_id` + `feedback_token` are present **only when `CHAT_FEEDBACK_ENABLED=true` AND the request carried `X-User-Id`** (B51, T-FB.10). Both omitted otherwise — clients should treat them as optional and skip the `/feedback/v1` UI when absent.

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
- **S6b** — Request without `role:"system"` entry → server prepends the default system message before LLM invocation; observable via mock LLM capture.
- **S6c** — Request with only `messages` → defaults (`provider="openai"`, `model="gptoss-120b"`, `temperature=0.7`, `maxTokens=4096`) applied.
- **S6d** — Empty retrieval (index empty or retriever error) → response `sources: null` and the LLM still answers.
- **S6e** — Every emitted `sources[]` entry has all six fields populated and `type="knowledge"`.
- **S6j orphan chunk dropped (B36)** — ES chunk with no `READY` `documents` row is dropped by `_SourceHydrator`; never reaches `sources[]` or LLM context (applies to `/chat`, `/chat/stream`, `/retrieve`).
- **S6f filter by source_app (B29)** — Both retrievers apply `term: {source_app}` filter; `sources[]` contains only matching rows.
- **S6g filter AND (B29→B35)** — Both `source_app` and `source_meta` supplied → AND filter; only rows matching both appear.
- **S6h filter no-match (B29)** — No matching chunks → `sources: null`; LLM still answers.
- **S6i empty filter rejected (B29)** — `source_app=""` → 422 `CHAT_FILTER_INVALID`; no LLM call.

---

#### 3.4.4 `POST /retrieve` — Retrieval without LLM

Runs the full retrieval pipeline (embed → kNN + BM25 → RRF join → source hydration) and returns ranked chunks without invoking the LLM. Useful for retrieval quality inspection and custom UIs.

**Request schema:**

```json
{
  "query":            "What are our Q3 OKRs?",
  "source_app":       "confluence",
  "source_meta":      "engineering",
  "top_k":            20,
  "min_score":        0.3,
  "dedupe":           false
}
```

- Only `query` is required.
- `source_app` / `source_meta`: same optional ES `term` filters as `/chat` (B29 → B35).
- `top_k` (default `RETRIEVAL_TOP_K`, env-configurable, default 20): max chunks returned from the retrieval pipeline; range 1–200.
- `min_score` (default `null`): post-retrieval score threshold — chunks whose retrieval score is below this value are dropped. Applied after `pipeline.run()` (not passed to ES retrievers, which do not accept a score threshold param).
- `dedupe` (default `false`): when `true`, keeps only the highest-scored chunk per `document_id` (pipeline output is already score-sorted, so first-seen = best).

**Response schema:**

```json
{
  "chunks": [
    {
      "document_id":  "01J9...",
      "source_app":   "confluence",
      "source_id":    "DOC-123",
      "source_meta":  "engineering",
      "type":         "knowledge",
      "source_title": "Q3 OKR Planning",
      "source_url":   "https://wiki.example/q3-okr",
      "mime_type":    "text/markdown",
      "excerpt":      "...truncated to EXCERPT_MAX_CHARS...",
      "score":        0.87
    }
  ]
}
```

- `chunks` is an empty array when no results are found (never `null`).
- `excerpt` is truncated to `EXCERPT_MAX_CHARS` (default 512) in the router — same rule as `/chat` `sources[].excerpt` (B23).
- `dedupe=false`: the same `document_id` can appear multiple times if multiple chunks from the same document ranked highly.
- `dedupe=true`: one entry per `document_id`; the chunk with the highest RRF score is kept.

**BDD:**
- **S38 retrieve returns all chunks by default** — Given two chunks from the same `document_id` both rank in the top-K, When `POST /retrieve {"query":"..."}` (no `dedupe`), Then both appear in `chunks[]` with the same `document_id`.
- **S39 retrieve dedupe=true keeps best chunk** — Given the same two chunks, When `POST /retrieve {"query":"...","dedupe":true}`, Then exactly one entry with that `document_id` appears, and its `excerpt` matches the higher-scored chunk.
- **S40 retrieve empty index** — Given an empty ES index, When `POST /retrieve`, Then `{"chunks":[]}` is returned (not `null`).
- **S41 retrieve filter (B29 → B35)** — Same `source_app` / `source_meta` filter semantics as `/chat`; non-matching filter returns `{"chunks":[]}`.

- **S36 CJK BM25 via icu_tokenizer (B26)** — Given a document body containing `"產品規格"` (no whitespace) indexed under the `icu_text` analyzer, When a chat query for `"產品規格"` runs against `chunks_v1`, Then the BM25 retriever returns the chunk; the same query against a `standard`-analyzed control index does not. Proves the analyzer choice (B26) is functionally required for CJK retrieval.
- **S37 chat rate-limit per user (B31)** — Given `CHAT_RATE_LIMIT_PER_MINUTE=N` and a single `X-User-Id`, When the same caller issues N+1 `POST /chat` (or `/chat/stream`) requests within the window, Then the first N succeed and the (N+1)th returns 429 `application/problem+json` with `error_code=CHAT_RATE_LIMITED` and a `Retry-After` header equal to seconds until window reset. A different `X-User-Id` gets an independent budget; ingest, MCP, and health endpoints are unaffected. After the window expires the counter resets.
- ~~**S8**~~ — Superseded by S58–S67 (§3.8) in P2.5; the `POST /mcp/v1/tools/rag` 501 stub was removed.

---

#### 3.4.5 `POST /feedback/v1` — User feedback on chat sources (B50/B51/B52, T-FB.6)

Closes the feedback loop: client echoes back the HMAC-signed token from a prior `/chat` response and reports a vote against one of the shown sources. Dual-writes MariaDB `feedback` (§5.1) and ES `feedback_v1` (§5.4); next `/chat` consults the ES index via `_FeedbackMemoryRetriever` (B50) when `CHAT_FEEDBACK_ENABLED=true`.

**Request schema:**

```json
{
  "request_id":     "01J9...",
  "feedback_token": "<base64url>.<hmac_hex>",
  "query_text":     "what are our Q3 OKRs?",
  "shown_sources":  [
    {"source_app": "confluence", "source_id": "DOC-A"},
    {"source_app": "confluence", "source_id": "DOC-B"},
    {"source_app": "drive",      "source_id": "DOC-C"}
  ],
  "source_app":     "confluence",
  "source_id":      "DOC-A",
  "vote":           1,
  "reason":         "irrelevant",
  "position_shown": 0
}
```

- `request_id`, `feedback_token`: from the `/chat` response (§3.4.2). Token TTL = 7 days. The token's signed `request_id` and `user_id` are **authoritative**; the body's `request_id` must equal the signed value, and (if `X-User-Id` is present) it must equal the token's `user_id`. Mismatch → 401 `FEEDBACK_TOKEN_INVALID`.
- `query_text`, `shown_sources`: re-supplied by client; HMAC over `sources_hash = sha256(json([[source_app, source_id], …]))` binds the **pair** list (document identity is `(source_app, source_id)` per B11/B35).
- `(source_app, source_id)` of the voted source ∈ `shown_sources` (server-enforced).
- `vote` ∈ {+1, -1}.
- `reason` ∈ {`irrelevant`, `hallucinated`, `outdated`, `incomplete`, `wrong_citation`, `other`} (B52, frozen) or omitted.
- `position_shown`: 0-based index of the voted source in the original `/chat sources[]` (recorded for future IPS; B53 item 1, unused in P1).

**Response:** `204 No Content` on success; `application/problem+json` on error per §4.1.1.

**Errors:**
- `401 FEEDBACK_TOKEN_INVALID` — HMAC mismatch, malformed token, `request_id` mismatch with signed value, `X-User-Id` mismatch with signed `user_id`, or `shown_sources` doesn't match the signed `sources_hash`.
- `410 FEEDBACK_TOKEN_EXPIRED` — token `ts` outside the 7-day window.
- `422 FEEDBACK_SOURCE_INVALID` — voted `(source_app, source_id)` pair not in `shown_sources`.
- `422 FEEDBACK_VALIDATION` — schema violations (vote ∉ {±1}, reason outside enum, missing required field).

**Dual-write semantics (B51):**
1. MariaDB `feedback` UPSERT keyed on `(user_id, request_id, source_app, source_id)` — idempotent; second vote overwrites.
2. ES `feedback_v1` index with `_id = sha256(user_id|request_id|source_app|source_id)` — same idempotency. Re-embeds `query_text` once per call via `EmbeddingClient.embed([query_text], query=True)`. ES doc carries `source_app` (`_FeedbackMemoryRetriever` aggregates by the pair).
3. ES leg failure logs `event=feedback.es_write_failed` and increments `ragent_feedback_es_write_failed_total`; the request still returns 204 because MariaDB is the truth.

**BDD:**
- **S42 happy** — Valid token + valid shape → 204; MariaDB row exists; ES doc indexed with `source_app` field present.
- **S43 tampered token** — Single byte flip → 401 `FEEDBACK_TOKEN_INVALID`.
- **S44 expired token** — `ts` > 7 days ago → 410 `FEEDBACK_TOKEN_EXPIRED`.
- **S45 sources_hash mismatch** — Client supplies different `shown_sources` than at sign time → 401 `FEEDBACK_TOKEN_INVALID`.
- **S46 source not shown** — Voted `(source_app, source_id)` pair not in `shown_sources` → 422 `FEEDBACK_SOURCE_INVALID`.
- **S47 reason enum** — Reason outside B52 frozen set → 422.
- **S48 idempotent re-vote** — Same `(user_id, request_id, source_app, source_id)` posted twice → both return 204, single MariaDB row with `updated_at` advanced.
- **S49 ES fail-open** — ES write raises → 204 + `ragent_feedback_es_write_failed_total += 1`.
- **S50 request_id replay rejected** — Body `request_id` differs from signed payload `request_id` → 401 `FEEDBACK_TOKEN_INVALID`; no MariaDB/ES write.
- **S51 cross-user reuse rejected** — `X-User-Id` differs from signed `user_id` → 401 `FEEDBACK_TOKEN_INVALID`; no write.
- **S52 chat filter scope honoured** — `/chat` with `source_app=confluence` AND `CHAT_FEEDBACK_ENABLED=true` → `_FeedbackMemoryRetriever`'s kNN filter contains `term: {source_app: confluence}` (no boosting from other-app likes leaks into the response).

---

### 3.5 Authentication & Permission

Two distinct concerns, kept architecturally separate from retrieval:

| Concern | Question answered | Mechanism | P1 | Future phase |
|---|---|---|---|---|
| **Authentication** | Who is the caller? | JWT verify (`exp` expiry check) → `user_id = preferred_username` claim | OFF — `X-User-Id` header trusted, validated non-empty | JWT validated via FastAPI dependency; `RAGENT_TRUST_X_USER_ID_HEADER=true` falls back to header (dev/integration override) |
| **Permission** | Can this caller see this document? | Permission Layer service that calls **OpenFGA** | OPEN — no checks, all docs visible | `PermissionClient.batch_check(user_id, document_ids)` returns the allowed subset; gated per-surface by `RAGENT_PERMISSION_INGEST_ENABLED` / `RAGENT_PERMISSION_CHAT_ENABLED` (both default `false` even in P2) |

**Design principle:** ES (`chunks_v1`) carries **no auth fields** in any phase — retrieval is permission-blind. The Permission Layer post-filters by `document_id`, keeping ES schema stable across phases.

**P1 (current phase):** No JWT — `X-User-Id` trusted, written to `documents.create_user` (audit only, not authz). No permission gating — all chunks visible. `auth_mode=open` in audit logs. **TokenManager (J1→J2) is active** for Embedding/LLM/Rerank API auth (unrelated to user auth).

**P2 additions:**
- **JWT:** `{"exp": <unix-epoch>, "preferred_username": "<user_id>"}`. Absent/expired/invalid → 401 with `AUTH_CLAIM_MISSING` / `AUTH_TOKEN_EXPIRED` / `AUTH_TOKEN_INVALID`. `RAGENT_TRUST_X_USER_ID_HEADER=true` (non-prod only) bypasses JWT.
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
- **Structured logging (structlog).** JSON to stdout. Categories: (1) **API trace** (`api.request/error`) — per-request `{request_id, method, path, status_code, duration_ms, user_id, trace_id}` via `RequestLoggingMiddleware` (excl. /livez, /readyz, /metrics). (2) **Business** — `chat.retrieval/llm`, `ingest.failed/ready`, `reconciler.tick`, `embedding/rerank.call`, etc., paired with OTEL spans sharing `trace_id`. (3) **Error** — `error_type, error_code`, traceback, redacted. Format: ISO 8601 UTC; `LOG_FORMAT=console` for dev. **Privacy:** identity + metric fields only; denylist processor drops `query/prompt/messages/completion/chunks/embedding/documents/body/authorization/cookie/password/token/secret` and stamps `content_redacted=true`. `HAYSTACK_CONTENT_TRACING_ENABLED` pinned off.

---

### 3.8 MCP Tool Server (P2.5)

Exposes ragent's retrieval pipeline as a **Model Context Protocol** tool so external LLM agents (Claude Desktop, Cursor, in-house agents) can call ragent's corpus through the MCP standard rather than a bespoke HTTP shape. The MCP server **wraps `POST /retrieve/v1`** (§3.4.4) — it does NOT call the LLM. The calling agent's own LLM does the synthesis; ragent supplies the grounded chunks.

**Decision (B47):** P2.5 implements a **real MCP server speaking JSON-RPC 2.0** (not the P1 stub's REST shape). The P1 `POST /mcp/v1/tools/rag` 501 endpoint is **removed** and replaced by `POST /mcp/v1` carrying JSON-RPC envelopes. This is the user-requested Option B (full MCP, retrieve-only). Option A (REST tool-call) and Option C (REST + thin MCP shim) were rejected because they either misrepresent the protocol (A) or carry two surfaces with the same behavior (C).

#### 3.8.1 Protocol

- **Transport:** Streamable HTTP, request/response subset (POST only; no server-initiated SSE in P2.5). Pinned MCP spec revision: `"2024-11-05"`.
- **Endpoint:** `POST /mcp/v1` (single endpoint; method dispatched from JSON-RPC `method` field).
- **Envelope:** JSON-RPC 2.0:
  ```json
  // Request
  {"jsonrpc": "2.0", "id": <int|str|null>, "method": "<method>", "params": {...}}
  // Success response
  {"jsonrpc": "2.0", "id": <same-as-request>, "result": {...}}
  // Error response
  {"jsonrpc": "2.0", "id": <same-as-request>, "error": {"code": <int>, "message": "<text>", "data": {...}?}}
  ```
- **Notification** (no response): omit `id`. P2.5 supports `notifications/initialized` only.
- **Auth:** `Authorization: Bearer <jwt>` (P2.2 onwards) or `X-User-Id` fallback (`RAGENT_TRUST_X_USER_ID_HEADER=true`, dev only). Auth applies before JSON-RPC dispatch; failure returns HTTP 401 with `application/problem+json` (NOT a JSON-RPC error — auth is a transport-layer concern).
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

The sole tool advertised by `tools/list`. Mirrors §3.4.4 `POST /retrieve/v1` semantics:

```json
{
  "name": "retrieve",
  "description": "Retrieve relevant document chunks from the ragent corpus using hybrid vector+BM25 search with optional reranking. Returns ranked chunks (no LLM synthesis).",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":       {"type": "string", "minLength": 1, "description": "Natural-language query."},
      "top_k":       {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
      "source_app":  {"type": "string",  "minLength": 1, "maxLength": 64,   "description": "Optional ES term filter."},
      "source_meta": {"type": "string",  "minLength": 1, "maxLength": 1024, "description": "Optional ES term filter."},
      "min_score":   {"type": "number",  "minimum": 0,    "description": "Optional post-pipeline score floor."},
      "dedupe":      {"type": "boolean", "default": false, "description": "Keep one chunk per document_id."}
    },
    "required": ["query"]
  }
}
```

**`tools/call` result shape** (MCP spec compliant):
```json
{
  "content": [
    {"type": "text", "text": "{\"chunks\":[{...},{...}]}"}
  ],
  "isError": false
}
```

The single `content[0].text` value is the **JSON-stringified** `RetrieveResponse` (same shape as `POST /retrieve/v1`). MCP standardises tool-result content as a typed array; text type with stringified JSON is the canonical pattern for structured returns (the calling LLM parses it). `isError: true` is set when the tool itself fails (e.g. retrieval pipeline raises); transport-layer failures still come through `error` envelopes.

#### 3.8.4 Error codes (JSON-RPC layer)

| Code | Meaning | Origin |
|---|---|---|
| `-32700` | Parse error (malformed JSON) | Transport |
| `-32600` | Invalid Request (missing `jsonrpc` / `method`, etc.) | Transport |
| `-32601` | Method not found | Dispatch |
| `-32602` | Invalid params (e.g. `tools/call` with unknown `name`, or `inputSchema` validation fail) | Dispatch |
| `-32603` | Internal error | Server |
| `-32001` | Tool execution failed (retrieval pipeline error; mirrors `MCP_TOOL_EXECUTION_FAILED`) | App |

App-level errors (-32000..-32099) carry `data.error_code` matching the existing `HttpErrorCode` catalog (§4.1.2) so operators correlate JSON-RPC errors with HTTP errors. Example:
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"retrieval pipeline failed","data":{"error_code":"MCP_TOOL_EXECUTION_FAILED"}}}
```

#### 3.8.5 BDD

- **S58 mcp initialize** — `initialize` with `protocolVersion:"2024-11-05"` → `result.{protocolVersion:"2024-11-05", capabilities:{tools:{}}, serverInfo:{name:"ragent",version:"<semver>"}}`.
- **S59 mcp tools/list** — `result.tools` has exactly one entry `name:"retrieve"` with `inputSchema` matching §3.8.3.
- **S60 mcp tools/call retrieve** — Given indexed corpus and `tools/call` with `{name:"retrieve", arguments:{query:"...",top_k:3}}`, When the server processes it, Then `result.content[0].text` is JSON parseable into `{chunks: list}` of length ≤ 3 and `result.isError` is `false`.
- **S61 mcp method not found** — Given `{method:"resources/list"}` (unimplemented), Then `error.code` is `-32601`.
- **S62 mcp tools/call invalid name** — Given `{method:"tools/call", params:{name:"unknown_tool",arguments:{}}}`, Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_NOT_FOUND`.
- **S63 mcp tools/call missing query** — Given `{method:"tools/call", params:{name:"retrieve",arguments:{}}}` (no `query`), Then `error.code` is `-32602` and `error.data.error_code` is `MCP_TOOL_INPUT_INVALID`.
- **S64 mcp parse error** — Given a request body that is not valid JSON, Then HTTP `200` with JSON-RPC body `{jsonrpc:"2.0",id:null,error:{code:-32700,...}}` (per JSON-RPC 2.0 §5: `id` is `null` when parse failed).
- **S65 mcp notifications/initialized** — Given `{jsonrpc:"2.0", method:"notifications/initialized"}` (no `id`), Then HTTP `204` with empty body; no JSON-RPC response object emitted.
- **S66 mcp auth required** — Given `RAGENT_AUTH_DISABLED=false` and no `Authorization` header, Then HTTP `401` with `application/problem+json` (NOT a JSON-RPC error envelope) and `error_code=AUTH_CLAIM_MISSING`.
- **S67 mcp tool retrieval failure** — Given the retrieval pipeline raises, When `tools/call retrieve` is invoked, Then JSON-RPC response is `{error:{code:-32001, message:..., data:{error_code:"MCP_TOOL_EXECUTION_FAILED"}}}` — NOT `isError:true` inside a successful result. (App-error vs tool-soft-error distinction: pipeline crashes are JSON-RPC errors; an empty-result-set retrieval is `isError:false` with empty `chunks`.)

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
> Validation order: discriminator-shape (422) → `mime_type ∈ {text/plain,text/markdown,text/html}` (415) → inline `len(content.encode("utf-8")) ≤ INGEST_INLINE_MAX_BYTES` / file HEAD-probe size ≤ `INGEST_FILE_MAX_BYTES` (413) → `minio_site` resolved against `MinioSiteRegistry` (422 `INGEST_MINIO_SITE_UNKNOWN`) → file HEAD-probe object exists (422 `INGEST_OBJECT_NOT_FOUND`). For `POST /ingest/v1/upload`: magic-byte signature check after `file.read()` (415 `INGEST_MAGIC_MISMATCH`) for binary MIMEs (docx/pptx/pdf). Worker-side guards run before splitter parse: DOCX/PPTX zip preflight (`INGEST_MAX_ARCHIVE_MEMBERS` / `_RATIO` / `_EXPANDED_BYTES`) → 413 `INGEST_ARCHIVE_UNSAFE` persisted as `documents.error_code` with terminal `FAILED`; PDF page-count cap (`INGEST_MAX_PDF_PAGES`) → 413 `INGEST_PDF_TOO_MANY_PAGES` likewise. Every guard rejection increments `ragent_ingest_rejected_total{reason}` (T-SEC.7).

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
| POST   | `/mcp/v1`               | `X-User-Id` (P1) / `Authorization: Bearer` (P2) | JSON-RPC 2.0 envelope per §3.8 | `200` with JSON-RPC response envelope; `204` for `notifications/*`. Auth failure (401) returns `application/problem+json` per §3.8.1 (transport-layer). |
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
| `INGEST_MAGIC_MISMATCH`              | 415         | `POST /ingest/v1/upload`: declared `mime_type` (docx/pptx/pdf) does not match the file's leading signature bytes (T-SEC.1/.2) | Router T-SEC.2 |
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
| `FEEDBACK_TOKEN_INVALID`             | 401         | HMAC mismatch, malformed token, or `shown_source_ids` doesn't match the signed `sources_hash` (T-FB.6, B51) | Router (feedback) |
| `FEEDBACK_TOKEN_EXPIRED`             | 410         | Token `ts` outside the 7-day window (T-FB.6, B51) | Router (feedback) |
| `FEEDBACK_SOURCE_INVALID`            | 422         | `source_id ∉ shown_source_ids` (T-FB.6) | Router (feedback) |
| `FEEDBACK_VALIDATION`                | 422         | Schema violations: vote ∉ {±1}, reason outside B52 enum, missing required field | Schema (feedback) |
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
| `AUTH_TOKEN_EXPIRED`                 | 401             | JWT `exp` claim is in the past (T8.1) | Auth dependency T8.2 |
| `AUTH_CLAIM_MISSING`                 | 401             | `exp` or `preferred_username` claim absent or empty (T8.1) | Auth dependency T8.2 |
| `AUTH_TOKEN_INVALID`                 | 401             | JWT signature invalid, or `exp` non-numeric/non-integer (T8.1) | Auth dependency T8.2 |

### 4.2 Supported Formats

| Format | Converter | MIME (allow-list) | Notes | Phase |
|---|---|---|---|:---:|
| `.txt`  | `TextFileToDocument`     | `text/plain`              | UTF-8 text | **P1** |
| `.md`   | `MarkdownToDocument`     | `text/markdown`           | front-matter stripped | **P1** |
| `.html` | `HTMLToDocument`         | `text/html`               | visible text, script/style stripped | **P1** |
| `.csv`  | `CSVToDocument`          | `text/csv`                | row-as-document; rows packed by `RowMerger` to ~2 000 chars (B24); bounded by global 50 MB file limit (B2) | **P1** |
| `.pdf`  | `_PdfASTSplitter`        | `application/pdf`         | one atom per page; fast MuPDF text extraction; Tesseract OCR on image-bearing pages; batch-safe for large files | **P1** |
| `.docx` | `_DocxASTSplitter`       | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | paragraphs + tables (python-docx) | **P1** |
| `.pptx` | `_PptxASTSplitter`       | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | one atom per slide (python-pptx) | **P1** |
| `.xlsx` | `XLSXToDocument`         | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | active sheets | P2 |

> 415 on unsupported MIME; 413 on > 50 MB. PDF ingest supports scanned / image-bearing pages via Tesseract OCR (requires OS-level `tesseract`); pages with no raster images use the fast MuPDF text path (no Tesseract cost).

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

> **Inventory rules (B28):** every external dependency, every per-call timeout, every operational threshold, and every credential MUST appear in this table. Code that reads a literal value not represented here is a spec drift bug. Vars marked `(required)` have no default and refuse boot.

> **v2 removed vars (C6):** `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/SECURE/BUCKET` (→ `MINIO_SITES`), `INGEST_MAX_FILE_SIZE_BYTES` (→ `INGEST_INLINE/FILE_MAX_BYTES`), `CHUNK_TARGET_CHARS_EN/CJK/CSV`, `CHUNK_OVERLAP_CHARS_EN/CJK/CSV`, `CHUNK_HARD_SPLIT_OVERLAP_CHARS`.

#### 4.6.1 Bootstrap & HTTP server

| Variable | Default | Description |
|---|---|---|
| `RAGENT_ENV`                          | (required)       | `dev` \| `staging` \| `prod`. P1 startup guard refuses non-`dev`. |
| `RAGENT_AUTH_DISABLED`                | `false`          | Must be `true` in P1; removed in P2 to enable JWT (§3.5). |
| `RAGENT_TRUST_X_USER_ID_HEADER`       | `false`          | **P2 only.** When `true` and `RAGENT_ENV != prod`, JWT dependency is bypassed and the `X-User-Id` header is trusted as `preferred_username` (§3.5). Strictly ignored in `prod`. |
| `RAGENT_PERMISSION_INGEST_ENABLED`    | `false`          | **P2 only.** When `true`, `GET/DELETE /ingest/v1/{id}` and `GET /ingest/v1` enforce `PermissionClient` (§3.5). Default off — gate is wired but inert until OpenFGA tuples exist. |
| `RAGENT_PERMISSION_CHAT_ENABLED`      | `false`          | **P2 only.** When `true`, chat retrieval applies the `PermissionClient` post-filter (§3.5). Default off. |
| `RAGENT_HOST`                         | `127.0.0.1`      | API bind address. P1 OPEN guard (§1) refuses any value other than `127.0.0.1` while `RAGENT_ENV=dev` & `RAGENT_AUTH_DISABLED=true`. |
| `RAGENT_PORT`                         | `8000`           | API bind port. |
| `LOG_LEVEL`                           | `INFO`           | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. Applies to app + TaskIQ + Reconciler. |
| `CORS_ALLOW_ORIGINS`                  | *(unset)*        | Comma-separated list of allowed CORS origins (e.g. `https://app.example.com,https://admin.example.com`). When unset or empty, no `CORSMiddleware` is added and all cross-origin requests are denied. |

#### 4.6.2 Datastore connections (boot-blocking)

| Variable | Default | Description |
|---|---|---|
| `MARIADB_DSN`                         | (required)       | Full SQLAlchemy DSN, e.g. `mysql+aiomysql://user:pass@host:3306/ragent?charset=utf8mb4`. Used by repositories, bootstrap, `/readyz`. |
| `MARIADB_POOL_RECYCLE_SECONDS`        | `280`            | SQLAlchemy `pool_recycle` value. Connections older than this are discarded on checkout. Must be less than the server-side `wait_timeout`; default 280 s assumes a 300 s server timeout. |
| `ES_HOSTS`                            | (required)       | Comma-separated `https?://host:port` list. |
| `ES_USERNAME`                         | (optional)       | Basic-auth username; omit for unauthenticated dev clusters. |
| `ES_PASSWORD`                         | (optional)       | Basic-auth password. |
| `ES_API_KEY`                          | (optional)       | Alternative to user/password (mutually exclusive). |
| `ES_VERIFY_CERTS`                     | `true`           | Set `false` for self-signed dev clusters. |
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
| `UNPROTECT_ENABLED`                   | `false`          | When `true`, worker calls the unprotect API before passing file bytes to the ingest pipeline. |
| `UNPROTECT_API_URL`                   | (required when enabled) | Full URL of the unprotect endpoint (multipart POST). |
| `UNPROTECT_APIKEY`                    | (required when enabled) | Raw JWT (no `Bearer` prefix) sent as `apikey` request header. **Never logged, never echoed.** |
| `UNPROTECT_DELEGATED_USER_SUFFIX`     | (required when enabled) | Appended to `X-User-Id` to form the `delegatedUser` form field: `{X-User-Id}{suffix}`. |

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
| `RAGENT_DEFAULT_SYSTEM_PROMPT`        | `You are a helpful assistant` | Auto-prepended when `messages` lacks a `system` entry. |
| `RAGENT_DEFAULT_RAG_SYSTEM_PROMPT`    | *(multi-intent template)*     | System prompt used when retrieval returns ≥1 doc and the caller has no `system` message. Contains grounding rules + QUESTION/SUMMARY/GENERATION intent styles with few-shot examples. No `{context}` placeholder — context is injected into the user message. |
| `RAGENT_RAG_GROUNDING_RULES`          | *(rules-only variant)*        | Rules-only system prompt prepended when the caller supplies their own `system` message alongside retrieved docs. Preserves the caller's persona while enforcing context-only grounding. |
| `CHAT_RATE_LIMIT_PER_MINUTE`          | `30`             | Per-user request cap on `/chat/v1` + `/chat/v1/stream` within the rate-limit window (B31). Excess returns 429 `CHAT_RATE_LIMITED`. |
| `CHAT_RATE_LIMIT_WINDOW_SECONDS`      | `60`             | Fixed-window length for `CHAT_RATE_LIMIT_PER_MINUTE` (B31). |
| `MCP_REQUEST_MAX_BYTES`               | `262144` (256 KiB) | Defence-in-depth cap on `POST /mcp/v1` request bodies; over-limit returns HTTP 413 `application/problem+json` (§3.8.1). |
| `CHAT_FEEDBACK_ENABLED`               | `false`          | Master switch for the feedback retrieval signal (B50). `true` enables `POST /feedback/v1`, the `_FeedbackMemoryRetriever` 3rd RRF input, and requires `FEEDBACK_HMAC_SECRET`. Default off — ship dark, observe write volume first (B53). |
| `CHAT_FEEDBACK_RRF_WEIGHT`            | `0.5`            | Weight on the feedback retriever's contribution in `DocumentJoiner(weights=[1.0, 1.0, this])` (B50). Cap < 1.0 to prevent popularity-loop dominance. |
| `CHAT_FEEDBACK_MIN_VOTES`             | `3`              | `(likes + dislikes)` threshold below which a (source_app, source_id) is dropped from the retriever (B50). Defeats single-user signal poisoning. |
| `CHAT_FEEDBACK_HALF_LIFE_DAYS`        | `14`             | Score decay half-life applied to the per-source Wilson score: `score × 0.5 ** (age_days / this)` (B50). |
| `FEEDBACK_HMAC_SECRET`                | *(required when `CHAT_FEEDBACK_ENABLED=true`)* | HMAC key for signing `/chat` response tokens and verifying `POST /feedback/v1` payloads (B51). Boot fails when feedback is enabled but the secret is unset. |
| `PDF_OCR_LANGUAGES`                   | `eng+chi_sim+chi_tra+jpn+deu` | Tesseract language pack list (plus-separated) used by `_PdfASTSplitter` when OCR-ing image-bearing pages. Install matching `tesseract-ocr-<lang>` packages if non-default languages are added. |

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

> **v2 OVERRIDE** — `documents` adds `ingest_type ENUM('inline','file') NOT NULL DEFAULT 'inline'`, `minio_site VARCHAR(64) NULL`, `source_url VARCHAR(2048) NULL`. The **`chunks` table is dropped** — chunks live only in ES `chunks_v1`. `object_key` semantics: for `inline` it points into `__default__` MinIO site; for `file` it is the caller-supplied key in the named site (no copy).

```sql
CREATE TABLE documents (
  document_id      CHAR(26)     PRIMARY KEY,
  create_user      VARCHAR(64)  NOT NULL,
  source_id        VARCHAR(128) NOT NULL,
  source_app       VARCHAR(64)  NOT NULL,
  source_title     VARCHAR(256) NOT NULL,
  source_meta      VARCHAR(1024) NULL,
  ingest_type      ENUM('inline','file')  NOT NULL DEFAULT 'inline',
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
  reason          VARCHAR(32)  NULL,                    -- B52 enum (6 values) or NULL
  position_shown  SMALLINT     NULL,                    -- for future IPS (B53 item 1) — collected, unused in P1
  created_at      DATETIME(6)  NOT NULL,
  updated_at      DATETIME(6)  NOT NULL,
  UNIQUE KEY uq_user_req_app_src (user_id, request_id, source_app, source_id),
  CONSTRAINT ck_vote_unit CHECK (vote IN (-1, 1))
);
-- Append-only event log. ES `feedback_v1` (§5.4) is the derived serving view (B50/B51).
-- No content/text — query_text lives only in `feedback_v1` per the "text in ES, meta in MariaDB" rule.
```

No physical FK. ORM-level cascade only.

**ID classification:**
- **Internal IDs** (UID rule applies — `00_rule.md` §ID Generation Strategy): `document_id`, `chunk_id` — `CHAR(26)` UUIDv7→Crockford Base32, generated by `new_id()`.
- **External IDs / display fields** (UID rule does **not** apply — supplied by clients or upstream systems): `source_id` (≤ 128 chars; client-supplied stable identifier), `source_app` (≤ 64 chars; namespace/source system), `source_title` (≤ 256 chars; human-readable title surfaced as `sources[].source_title` in chat/retrieve responses), `source_meta` (optional free-format ≤ 1024 chars; renamed from `source_workspace` per B35), `create_user` (≤ 64 chars; audit metadata only, **not an authorization field**).
- The `task_id` returned from `POST /ingest` is the `document_id` itself; no separate task identifier exists.

### 5.2 Elasticsearch `chunks_v1`

> **v2 OVERRIDE** — adds `raw_content` field (`type: text, index: false`, `_source`-only). `content` (existing `text` column, may also be exposed under that legacy name) holds the **normalized** view embedded by bge-m3 + BM25-analyzed; `raw_content` holds the **original byte slice** the splitter captured (markdown fences, HTML tags, etc.). Chat retrieval scores against `content`, but the LLM context and `sources[].excerpt` use `raw_content` (with `content` fallback for legacy chunks). `source_url` is added as a `keyword` field for citation rendering.

> **Source of truth (B26):** `resources/es/chunks_v1.json` — settings + mappings, checked into git. Bootstrap (§6.1) reads this file and `PUT /chunks_v1` if the index does not exist. The block below is the canonical content; any drift between this spec snippet and the resource file is a CI failure (`tests/integration/test_es_resource_drift.py`).

```json
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
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
      "embedding": {
        "type": "dense_vector",
        "dims": 1024,
        "index": true,
        "similarity": "cosine",
        "index_options": { "type": "flat" }
      }
    }
  }
}
```

**Shard topology (B26):** `number_of_shards: 1` (single primary — sufficient for P1 corpus size, simpler routing); `number_of_replicas: 0` (no replicas — single-node dev/CI clusters reach `green` immediately; prod overrides via cluster-level template when HA is needed in P2).

**BM25 analyzer (B26):** `text` and `title` use the custom `icu_text` analyzer (`icu_tokenizer` + `icu_folding` + `lowercase`) — required for CJK tokenisation; the default `standard` analyzer collapses CJK to per-character or mega-tokens and breaks BM25. The `analysis-icu` plugin is a hard ES dependency, verified at `/readyz` (B26, I5). **Test override (B42):** integration tests run against a vanilla `elasticsearch:9.2.3` container without the plugin and load `tests/resources/es/chunks_v1.json` (standard analyzer) via the `RAGENT_ES_RESOURCES_DIR` env override; CJK BM25 behaviour is therefore not covered by integration tests and is validated by manual / staging smoke tests instead (see §7 Decision Log B42).

**Vector index (B26):** `embedding` uses `index_options.type = flat` — exact brute-force kNN, no graph build cost at write time, deterministic recall. P2 will revisit `bbq_hnsw` (Better Binary Quantization HNSW) once the corpus exceeds the size at which `flat` query latency stops meeting the chat budget.

**Title surface (B15):** `title` is denormalised onto every chunk row from `documents.source_title`. Two retrieval surfaces are derived from it:
1. **Lexical** — BM25 retriever runs `multi_match` on `["text", "title^2"]` (title boosted 2× over body) using the `icu_text` analyzer (B26).
2. **Semantic** — `embedding` is computed as `embed(f"{source_title}\n\n{chunk_text}")` at ingest time, so every chunk vector already carries title semantics. No separate `title_embedding` field is stored.

**Filter surface (B29 → B35):** `source_app` and `source_meta` are denormalised from `documents` onto every chunk row as `keyword` fields. Chat (§3.4.1) accepts optional `source_app` and `source_meta` filter params; when present they apply as ES `term` filters in **both** retrievers' `filter` clause (kNN `filter` and BM25 `bool.filter`). Filtering happens **before** scoring narrows the candidate pool, so top-K returned reflects the requested scope without over-fetch. These are **not auth fields** (B14): they are content-scope metadata, like `lang`. Permission gating remains a separate post-retrieval layer (§3.5).

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

**Indexing semantics (B50/B51):**
- `_id` = `sha256(user_id|request_id|source_app|source_id)` so re-votes overwrite (mirrors MariaDB `uq_user_req_app_src`).
- `query_embedding` produced at feedback-write time via `EmbeddingClient.embed([query_text], query=True)` — same model used by `/chat` retrieval so kNN similarity is comparable.
- `user_id_hash = sha256(user_id).hexdigest()` — defence-in-depth for ES dumps; plaintext `user_id` lives only in MariaDB (`feedback.user_id`).
- Test override (B42) at `tests/resources/es/feedback_v1.json` omits the ICU analyzer, identical otherwise.

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

**Module conventions:** No env reads outside `bootstrap/composition.py`. `@broker.task` decorators import from `ragent.bootstrap.broker` only. Plugins never import `pipelines/`, `routers/`, or HTTP layers. Routers → services → repositories (constructor-injected). `bootstrap/init_schema.py` applies `schema.sql` + `resources/es/*.json` idempotently; no inline DDL.

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
| **B23** | 2026-05-05 | Chat API | Where `sources[].excerpt` is truncated | **In the router** (`_build_sources` for `/chat`, `_to_chunk` for `/retrieve`), after retrieval — `EXCERPT_MAX_CHARS` (default 512) hard character cut. `SourceHydrator` returns full chunk content; the LLM receives the untruncated text; only the API response field is shortened. `EXCERPT_MAX_CHARS` is a public constant exported from `pipelines/chat.py` and imported by both routers. | Truncate inside `SourceHydrator` (LLM context is also cut — original P1 approach, reverted: reduces answer quality on long chunks without benefit to the API consumer); truncate in retriever (couples retrieval to display concerns); leave to client (full chunk surfaced to API consumer — bandwidth waste + potential text leakage). | §3.4.2 / §3.4.4 / T3.6 / T3.19 |
| **B25** | 2026-05-04 | Storage | `documents.storage_uri` stored full URI; bucket name is constant config | **Rename column to `object_key VARCHAR(256) NOT NULL`** (key only, format per B10). Bucket is read from `MINIO_BUCKET` env var (default `ragent`); reconstruct full URI on demand. Saves ~20 bytes/row, decouples row from bucket-rename ops, and makes a future bucket migration a config flip. | Keep full URI (rigid); store bucket per-row (rotation hell); URL-encode in object key (key+bucket separation already does the job). | §5.1 / T2.5 / T2.6 |
| **B26** | 2026-05-04 | ES | (a) BM25 analyzer; (b) vector index type; (c) where the index definition lives | **(a) `icu_text` custom analyzer** (`icu_tokenizer` + `icu_folding` + `lowercase`) on `text` and `title` — required for CJK tokenisation; `standard` analyzer collapses CJK to per-character or mega-tokens, breaking BM25. `analysis-icu` plugin is a hard ES dependency (verified at `/readyz`). **(b) `bbq_hnsw`** (Better Binary Quantization HNSW, ES 8.16+) on `embedding` — ~32× memory reduction at negligible recall cost; falls back to standard HNSW with `event=es.bbq_unsupported` log if cluster rejects. **(c) Source of truth = `resources/es/chunks_v1.json`** loaded by boot auto-init (§6.1) when the index does not exist; spec §5.2 mirrors the file in prose; CI drift test enforces equality. | Default `standard` analyzer (CJK becomes useless for BM25); `nori`/`smartcn` (per-language plugin sprawl, doesn't cover all CJK consistently); raw HNSW (4× more memory at our 1024 dims); inline mapping in Python code (every change is a code commit, no resource-file diffability). | §5.2 / §6.1 / T0.8d / T0.9 |
| **B27** | 2026-05-04 | Infra | Redis topology — single-instance vs Sentinel HA | **Per-instance toggle via `REDIS_MODE` env (`standalone` \| `sentinel`)**. Both broker and rate-limiter share the mode; standalone reads `REDIS_BROKER_URL` / `REDIS_RATELIMIT_URL`; sentinel reads `REDIS_SENTINEL_HOSTS` (shared quorum) + `REDIS_*_SENTINEL_MASTER` (per-instance master name). Connection layer dispatches on mode (`redis-py-sentinel` vs `redis-py`). Default `standalone` for dev/CI; prod sets `sentinel`. | Hardcode Sentinel (broken local dev); hardcode standalone (no prod HA story); per-instance independent mode (config matrix doubles, no real-world need). | §3.6 / §4.6 / T0.9 |
| **B31** | 2026-05-04 | Chat API | Rate-limit Redis was declared (B27) and probed by `/readyz` (T7.7) but had no consumer — dead infrastructure declaration. | **Per-user fixed-window rate limit on `/chat` and `/chat/stream`**: `CHAT_RATE_LIMIT_PER_MINUTE` (default 30) over `CHAT_RATE_LIMIT_WINDOW_SECONDS` (default 60). `RateLimiter` adapter (`clients/rate_limiter.py`, T3.14) uses `INCR` + `EXPIRE` on the rate-limit Redis instance with key `ratelimit:chat:{user_id}`. Composition root exports a FastAPI `Depends` factory; chat router declares `dependencies=[Depends(chat_rate_limit_dep)]` (T3.16) — router-level, **not** global middleware, so ingest / health / MCP are unaffected. Excess returns 429 `application/problem+json` with `error_code=CHAT_RATE_LIMITED` (§4.1.2) and a `Retry-After` header equal to seconds until window reset (S37). | (a) Drop the rate-limiter from P1 entirely (removes infrastructure declared in B27/B28; defers a defence against LLM-cost runaway to P2); (b) global middleware on every endpoint (ingest and health endpoints would compete for the same per-user budget — wrong scope); (c) sliding-window or token-bucket (more accurate but `INCR + EXPIRE` is one RTT; sliding window needs `ZADD + ZREMRANGEBYSCORE + ZCARD`, ~3× cost for marginal accuracy gain at this throughput). | §3.4 / §4.1.2 / §4.6 / §6.2 / B27 / T3.13 / T3.14 / T3.15 / T3.16 / T7.5a |
| **B30** | 2026-05-04 | Operator UX | What does an operator have to do to bring up the system end-to-end? | **Two-command quickstart**: `cp .env.example .env` → fill required vars → `python -m ragent.api` (T7.5d) and `python -m ragent.worker` (T7.5e). All else is automatic: schema/index auto-init runs from FastAPI lifespan + worker startup (T0.8d, idempotent); composition root (T7.5a) wires every dependency from env vars, no per-module env reads; TaskIQ broker module (T0.10) is the single import point for `@broker.task` decorators; `.env.example` (T0.11) is symmetric with spec §4.6 (drift test T0.11a). Project module layout fixed in §6.2 — every plan row produces exactly one file in that tree. Reconciler is K8s-only and not required for the local two-command path (recovery surface, not steady-state). E2E quickstart asserted by T7.2 launching the real entrypoint subprocesses, not internal scaffolding. | (a) Manual `alembic upgrade head` step before boot — adds an operator-facing migration command, defeats "two commands"; (b) per-module env reads — couples every module to env, blocks DI testing; (c) split broker module per task — multiple import paths, decorator misregistration risk; (d) no `.env.example` — operator reads spec §4.6 by hand, easy to miss required vars and discover at first failed request; (e) free-form module layout — names drift between plan and code, integration tests import wrong path. | §1 / §3.1 / §4.6 / §6.1 / §6.2 / T0.10 / T0.11 / T7.5 / T7.5a–f / T7.2 |
| **B29** | 2026-05-04 | Chat API | Optional retrieval filter by `source_app` / `source_workspace` | **Filter in ES via denormalised keyword fields.** `chunks_v1` mapping gains two `keyword` fields (`source_app`, `source_workspace`) populated by `VectorExtractor` from `documents` at ingest. Chat request schema (§3.4.1) accepts both as optional fields; when present they apply as ES `term` filter in **both** retrievers' `filter` clause (kNN `filter`, BM25 `bool.filter`). AND semantics when both supplied. Empty string ⇒ 422 `CHAT_FILTER_INVALID`. These are scope metadata, not auth fields (B14 distinction preserved); permission gating remains a separate post-retrieval layer (§3.5). | Post-retrieval filter via document JOIN in SourceHydrator (forces over-fetch with unbounded `K' = K × overfetch_factor` — narrow workspaces silently truncate); filter on `documents` only, retrieve all chunks then drop (defeats kNN top-K semantics); add a third retriever per filter combination (mapping bloat, no win). Pre-existing `chunks_v1` data does not exist (still pre-implementation), so single-version mapping update is safe; would otherwise require `chunks_v2` + reindex. | §3.4 / §3.4.1 / §4.3 / §4.4 / §5.2 / `resources/es/chunks_v1.json` / T1.9 / T1.12 / T3.5 |
| **B35** | 2026-05-07 | Schema | Rename `documents.source_workspace VARCHAR(64) NULL` to `source_meta VARCHAR(1024) NULL` (free-format). Supersedes the `source_workspace` naming and width chosen in B11/B29. | **Rename + widen.** Column on `documents`, denormalised keyword on `chunks_v1` (with `ignore_above: 1024`), Pydantic field on ingest/chat/retrieve schemas, all repository / service / worker / pipeline references. Validator caps stay tiered: `source_app` ≤ 64 (still a keyed namespace), `source_meta` ≤ 1024 (free-format). Migration `005_rename_source_workspace_to_source_meta.sql` does `ALTER TABLE … CHANGE COLUMN`; crossing the VARCHAR length-prefix boundary (≤255 vs >255) means MariaDB falls back to ALGORITHM=COPY (brief table lock on prod). ES mapping updated in `resources/es/chunks_v1.json`; existing clusters need a reindex on upgrade — fresh installs pick up the new mapping automatically via boot auto-init (B26). | (a) Keep `source_workspace` and stretch its semantics to "any string" — name lies about scope, every new caller has to read the spec to know it's free-format; (b) drop the field — caller-side metadata is a real need (slack channel, S3 prefix, generic tags) and B29 already wired it into retrieval filters; (c) add a parallel `source_meta` and keep `source_workspace` for compat — two near-identical columns, ambiguous which one drives the filter. | §3.1 / §3.4.1 / §3.4.4 / §4.1.2 / §4.3 / §4.4 / §5.1 / §5.2 / B11 / B29 / `migrations/005_*.sql` / `resources/es/chunks_v1.json` |
| **B32** | 2026-05-07 | Architecture | When to introduce the document/revision split (`documents` + `document_revisions` + `active_revision_id`). | **Defer to Phase 2.** Phase 1 closes the existing supersede bugs only (cascade through `self.delete`, DB-side survivor guard in `pop_oldest_loser_for_supersede`). The revision split is a multi-day track touching repository, service, worker, reconciler, ES mapping, retrieval pipeline, and API shape; it lands on its own branch with its own plan.md entries. Design captured in `docs/team/2026_05_07_revision_model_proposal.md` (motivation §1, schema §4, code surfaces §5). | (a) Land in Phase 1 — too large for current branch, blocks unrelated work; (b) skip entirely — leaves "two READY rows produce mixed retrieval results during reingest" as permanent UX bug, and embedding-model migration has no safe path; (c) build a smaller "active flag" instead of revisions — does not solve embedding-model coexistence (different dim → different ES index → needs a per-row model field anyway). | §3.1 / §3.4 / `docs/team/2026_05_07_revision_model_proposal.md` |
| **B33** | 2026-05-07 | Retrieval | Embedding-model migration query routing — when documents are split across two embedders during a rollout, how does retrieval pick the right model for the question embedding? | **Per-document active-model routing.** Resolve candidate document_ids → group by `active_revision.embedding_model` → embed the question once per distinct model in the candidate set (typically 1, at most 2 during rollout) → run kNN against the index whose mapping matches that model → merge + rerank. Enables canary / cohort migration: a subset of docs can run on `bge-m3-v2` while the rest stay on `bge-m3`, all queried correctly. Two parallel ES indexes (`chunks_v1` old-dim, `chunks_v2` new-dim) keyed by `embedding_model`. | (a) Bulk-flip global model — simple but precludes canary; whole corpus must be reembedded before any query traffic shifts, defeating the rollback story; (b) multi-vector single-index — only works if reembed reuses identical chunk text and all dims are equal, neither holds for real model changes; (c) embed question with both models always — doubles query latency permanently, even after migration completes. | §3.2 / §3.4 / `docs/team/2026_05_07_revision_model_proposal.md` §3.4 |
| **B36** | 2026-05-08 | Retrieval | `_SourceHydrator` semantics on hydration miss — should a chunk whose `document_id` has no matching READY row be dropped or passed through with empty source fields? | **Drop.** Hydrator becomes the correctness gate: orphan ES chunks (post-DELETE), mid-flight rows (PENDING/UPLOADED/FAILED), and demoted rows (DELETING) never reach LLM context or `sources[]`. Decouples retrieval correctness from cleanup completeness — `fan_out_delete` failures, reconciler outages, or revision demotion latency become disk-reclaim concerns, not user-visible bugs. Cost: retrieval result count may be lower than ES recall when stale chunks exist; this is the desired behaviour. | (a) Pass through with `source_title=null` (current pre-B36) — orphan chunk content reaches LLM verbatim, citations show "unknown", silently corrupts answers; (b) ES-side filter joining `documents` at query time — Haystack ES integration does not support cross-index joins, requires custom retriever; (c) defer to active_revision_id (P2 revision model) — ties P1 correctness to multi-day P2 track. | §3.4 / S6j / `pipelines/chat.py::_SourceHydrator` |
| **B37** | 2026-05-08 | Bootstrap | Should `composition.build_container()` still hard-require legacy `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` when `MINIO_SITES` JSON is set? | **No.** When `MINIO_SITES` is configured, the legacy single-site `MinIOClient` becomes redundant — `MinioSiteRegistry` covers every IO path. `/readyz` minio probe switches from `container.minio_client` to `container.minio_registry.default().client`. Operator following `.env.example` (which marks legacy three as DEPRECATED) can boot with only `MINIO_SITES` set. Legacy vars remain honoured when `MINIO_SITES` is absent — synthesised into a `__default__` entry by `MinioSiteRegistry.from_env()`. | (a) Keep current behaviour (both required) — contradicts `.env.example` DEPRECATED marker, every operator hits sys.exit on first boot; (b) drop legacy support entirely — breaks any caller still on single-site env; (c) make legacy the source of truth and synthesise `MINIO_SITES` from it — defeats v2 multi-site design. | §4.6.2 / B30 / T-RR.4 / T-RR.5 / T-RR.6 |
| **B38** | 2026-05-08 | Bootstrap | TokenManager J1→J2 exchange validation timing — first-request lazy or boot-time pre-warm? | **Boot-time pre-warm in `_check_infra_ready`.** Each `TokenManager` in `container.token_managers` runs `get_token()` during the lifespan startup probe; failure raises and aborts boot. A wrong `AI_API_AUTH_URL` or stale `AI_*_J1_TOKEN` surfaces before `/livez` returns 200, so a green readiness probe truly means the AI dependency chain is reachable. Current lazy behaviour is preserved beyond boot — refresh-margin logic still triggers on subsequent requests near expiry. | (a) Stay lazy (current) — `/livez` and `/readyz` both green while AI auth is broken; first chat or first ingest task 500s opaquely; (b) periodic background warm — adds a third long-lived task to manage; lazy already covers expiry; (c) probe at `/readyz` instead of `_check_infra_ready` — would refuse to serve traffic but boot still succeeds; conflates dependency outage (transient) with credentials misconfig (permanent). | §3.6 / `bootstrap/app.py::_check_infra_ready` / `clients/auth.py::TokenManager` / T-RR.7 / T-RR.8 |
| **B39** | 2026-05-08 | Ingest | When a worker finishes a re-ingest of an existing `(source_id, source_app)`, should the new doc's `READY` transition also atomically demote prior `READY` siblings, or stay deferred to reconciler-driven supersede? | **Atomic promote-and-demote in the same tx.** Worker's READY transition becomes `UPDATE … SET status='READY' WHERE document_id=:new AND status='PENDING'; UPDATE … SET status='DELETING' WHERE source_id=:src AND source_app=:app AND document_id != :new AND status='READY'`. Combined with B36, retrieval transitions to the new revision the moment the worker's tx commits — no race window where both old and new are READY and both retrievable. Reconciler still runs supersede, but only as belt-and-suspenders for the case where the worker dies between the two UPDATEs (the second is idempotent on resume). | (a) Status quo (reconciler tick supersede) — race window unbounded if reconciler stalls; users see new+old mixed in chat; (b) Phase 2 active_revision_id pointer — semantically cleaner but multi-day track and not required to close the race; (c) demote at promote time but in a separate tx — re-introduces a race window (smaller but non-zero). | §3.1 / §3.4 / B32 / T-RR.9 / T-RR.10 |
| **B40** | 2026-05-08 | Ingest | Should HTTP `DELETE /ingest/{id}` actually invoke `PluginRegistry.fan_out_delete`, or rely on B36 hydrator drop + reconciler reclaim? | **Yes — invoke synchronously.** Spec §3.1 step 1 already prescribes the cascade order; current implementation skips it because `IngestService._broker` (a `TaskiqDispatcher`) lacks `fan_out_delete`. Fix wires `container.registry` (`PluginRegistry`) into `IngestService` and removes the `_has_fan_out` introspection branch. ES chunks are purged in the request scope so disk reclaim does not depend on reconciler activity. Worst-case HTTP latency bounded by `PLUGIN_FAN_OUT_TIMEOUT_SECONDS` (default 60); ES `delete_by_query` is sub-second in practice. **B36 still required** — protects the failure path where fan_out partially completes and the row is gone before all chunks are. | (a) Keep skipping — relies entirely on reconciler + B36 to mask orphans; ES disk grows unbounded between reconciler ticks; (b) async dispatch via TaskIQ — replaces reconciler with broker as load-bearing retry surface (same reliability tier); (c) outbox table + sweeper — duplicates reconciler at table level. | §3.1 / B3 audit / T-RR.11 / T-RR.12 / T-RR.13 |
| **B41** | 2026-05-09 | Ingest | B39 closed the both-READY race for **in-order** worker completion, but if an older worker finishes after a newer revision was already created (or already promoted), naively demoting "any other READY sibling" lets the older revision incorrectly win until reconciler-driven supersede arbitrates. Should the worker promote be DB-arbitrated, or accept the residual window? | **DB-arbitrated promote.** `promote_to_ready_and_demote_siblings` does `SELECT document_id FROM documents WHERE source_id=:src AND source_app=:app AND status IN ('PENDING','READY') ORDER BY created_at DESC, document_id DESC LIMIT 1 FOR UPDATE` to elect the survivor. If caller is the survivor → promote + demote prior READY (B39 path). If not → self-demote PENDING → DELETING in the same tx; the worker also gates post-READY enrichment (`registry.fan_out`) on the returned `bool`. Result: retrieval correctness holds from the worker's tx alone for any worker completion order, and reconciler is **safety-net only** — never load-bearing for user-visible state. | (a) Status quo (B39 + reconciler arbitration) — leaves a window where retrieval flips to the older revision until reconciler tick; reconciler becomes load-bearing for correctness; (b) Reject promote when not survivor (raise) — worker would crash + retry forever on permanently-superseded docs; (c) `active_revision_id` pointer (Phase 2) — semantically cleaner but a multi-day track and not required to close this race. | §3.1 / B36 / B39 / T-RR.14 / T-RR.15 |
| **B42** | 2026-05-08 | Testing | Integration-test ES container has no `analysis-icu` plugin (vanilla `elasticsearch:9.2.3` from `testcontainers`); prod mapping (B26) uses `icu_text` analyzer that requires the plugin → naïvely loading the prod mapping into the test ES fails at index creation. | **Two mapping files, env-driven dir override.** Prod loads `resources/es/chunks_v1.json` (ICU). Tests load `tests/resources/es/chunks_v1.json` (default `standard` analyzer, structurally identical otherwise) by setting `RAGENT_ES_RESOURCES_DIR` in `tests/conftest.py`; `init_es()` reads this env and falls back to the prod path. Drift test (`test_es_resource_drift.py`) continues to pin `resources/es/chunks_v1.json` ↔ spec §5.2; a parallel test pins the test mapping file's structural equality (analyzer field + ICU `analysis` block being the only deltas). **Risk accepted:** CJK BM25 behaviour (S36) is **not** covered by integration tests under this setup; covered by manual / staging smoke against an ES with `analysis-icu` installed (`Dockerfile.es-test`). | (a) Build `Dockerfile.es-test` for every test run — ~30–60s docker build cost per CI cold cache, rejected as too heavy; (b) bake `standard` analyzer into the single prod mapping — defeats B26 (CJK retrieval breaks in prod); (c) parametrize analyzer name via env inside one mapping file (template substitution) — adds an env per substitution surface and obscures what prod actually ships with. | §5.2 / §7 / `resources/es/chunks_v1.json` / `tests/resources/es/chunks_v1.json` / `tests/conftest.py` / `src/ragent/bootstrap/init_schema.py` |
| **B34** | 2026-05-07 | Storage | Retention window for non-active revisions (when does the sweep job delete chunks of a superseded revision)? | **Layered audit window keyed on `document_revisions.retention_class ENUM('migration','reingest','none')`**: defaults `migration=∞` (or 90 d if storage-bound), `reingest=24h`, `none=0`. Sweep job in reconciler reads the per-class window. Subject to four operational guards: (1) explicit `DELETE /ingest/{id}` cascades all revisions regardless of class (compliance > retention); (2) capacity alarms with "drop oldest non-active first" fallback past disk threshold; (3) per-source override; (4) backup excludes non-active revisions. | (a) Single global 24h window — too aggressive for migration rollback (which is a pointer flip but only works while old chunks exist); (b) "never sweep" — unbounded ES disk growth, no answer for `right-to-be-forgotten`; (c) couple sweep to active-flip event (sweep immediately on demotion) — same loss-of-rollback as (a). | §3.1 / `docs/team/2026_05_07_revision_model_proposal.md` §6.1 |
| **B28** | 2026-05-04 | Config | Env-var inventory was incomplete — missing datastore connections (MariaDB/ES/MinIO host/creds), J1 client credentials, HTTP bind, OTEL exporter, retry/timeout policy knobs, upload limits, and log level; `RERANK_API_URL` was misspelled `REREANK_API_URL` | **Reorganise §4.6 into 8 subsections** (bootstrap, datastore, redis, third-party clients, worker/reconciler, pipeline/chat, per-call timeouts, observability). **Add 26 new vars** covering every previously implicit literal: `MARIADB_DSN`, `ES_HOSTS`/`ES_USERNAME`/`ES_PASSWORD`/`ES_API_KEY`/`ES_VERIFY_CERTS`, `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_SECURE`, `RAGENT_HOST`/`RAGENT_PORT`/`LOG_LEVEL`, `AI_API_CLIENT_ID`/`AI_API_CLIENT_SECRET`, `WORKER_MAX_ATTEMPTS`, `RECONCILER_PENDING_STALE_SECONDS`/`RECONCILER_UPLOADED_STALE_SECONDS`/`RECONCILER_DELETING_STALE_SECONDS`, `INGEST_MAX_FILE_SIZE_BYTES`, `INGEST_LIST_MAX_LIMIT`, the seven per-call timeouts (`EMBEDDER_INGEST/QUERY`, `ES_BULK/QUERY`, `MINIO_GET/PUT`, `LLM`, `PLUGIN_FAN_OUT`, `READYZ_PROBE`), plus four `OTEL_*` vars. **Fix typo** `REREANK_API_URL` → `RERANK_API_URL`. **Rename** ambiguous `RECONCILER_STALE_AFTER_SECONDS` to per-state `RECONCILER_PENDING_STALE_SECONDS` and add UPLOADED/DELETING siblings. **Change `MINIO_BUCKET` default** from `ragent-staging` → `ragent` (B10/B25 prose updated). Also adds an inventory rule: any literal value read by code that is not represented in §4.6 is a spec drift bug. | Leave datastore connections as "implicit per-environment overrides" (every operator reinvents the wheel; bootstrap module has no canonical names to read); expose only DSN-style strings for ES/MinIO too (forces credential concatenation in URLs, harder to rotate); keep timeouts as code constants only (violates J21 rule "every call site lists per-call timeout AND aggregate ceiling"); ship without J1 creds (TokenManager has a URL but no way to authenticate — boot succeeds but every embedder/LLM call fails on first request). | §1 / §3.1 / §3.6 / §3.7 / §4.5 / §4.6 / §6.1 / T0.8 |
| **B47** | 2026-05-11 | API/MCP | P1 reserved `POST /mcp/v1/tools/rag` as a 501 stub with REST-shape `{query: str}`. P2.5 needs a real handler. Three options: (A) keep REST shape, (B) full MCP JSON-RPC 2.0 server, (C) REST core + thin MCP wrapper. | **Option B — real MCP JSON-RPC 2.0 server** at `POST /mcp/v1`, single endpoint dispatching by `method` field. Implements `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping` (§3.8.2). Sole tool `retrieve` wraps the existing `POST /retrieve/v1` pipeline (NOT chat — calling agent's LLM does the synthesis). Transport: streamable HTTP request/response subset (no SSE in P2.5). Protocol revision pinned to `2024-11-05`. Stateless; no `Mcp-Session-Id` session. JSON-RPC errors carry `data.error_code` matching the existing `HttpErrorCode` catalog so JSON-RPC and HTTP errors correlate. Auth (401) is transport-layer `application/problem+json`, NOT a JSON-RPC error envelope. The P1 `/mcp/v1/tools/rag` 501 endpoint is **removed**. | (A) Keep REST shape under `/mcp/` URL: misrepresents the protocol — stock MCP clients (Claude Desktop, Cursor) cannot register the server. (C) REST + MCP wrapper: two surfaces with identical behavior duplicate test matrix; YAGNI until both client types are confirmed. (D) Stateful MCP with `Mcp-Session-Id`: adds session storage requirement; not needed for a single read-only tool. (E) Wrap chat pipeline instead of retrieve: confuses MCP semantics — tools return data, the calling LLM reasons; chat already does both inside ragent. | §3.8 / §4.1 / §4.1.2 / §4.6.6 / S58–S67 / P2.5 |
| **B49** | 2026-05-11 | SRE/QA | The §3.6 resilience claims (reconciler ≤ 10 min recovery, idempotent partial-failure handling, fail-open reranker, mid-stream error framing) ship as prose in spec but have no executable evidence beyond C1 (single worker-kill case, currently `xfail(run=False)`). Per journal 2026-05-08 E2E gate integrity rule, every spec-declared SLO needs a test in some automated gate. | **Chaos drill suite under `tests/e2e/test_chaos/test_C<N>_<scenario>.py`** — six cases C1–C6 covering worker SIGKILL, MariaDB↔ES split-brain, ES bulk 207 partial, rerank 5xx, LLM stream interrupt, MinIO transient 503 (matrix in §3.6.1). Gated by a **nightly CI lane** (not per-PR — slow + inject delays), each case asserts the same four invariants (terminal status, ES/DB consistency, OTEL spans present, `chaos_drill_outcome_total{case,outcome}` increment). C1 unblocks the existing `test_chaos_worker_kill.py` (lift `xfail(run=False)`); C2–C6 are new files. Acceptance: nightly green for ≥ 3 consecutive runs before P2.6 軌三 marked done. | (a) Per-PR gate: ~5–10 min overhead per PR + WireMock state pollution between cases. (b) Manual quarterly drills: same failure mode as the silent `pytest.skip()` problem (journal 2026-05-08) — no automated signal. (c) Single chaos test parameterised over all cases: a shared fixture failure cascades all 6 to red; per-case files isolate diagnosis. | §3.6.1 / §3.7 / P2.6 軌三 / journal 2026-05-08 E2E gate integrity |
| **B50** | 2026-05-16 | Retrieval | How to introduce user feedback (like/dislike) as a ranking signal without building a model-training pipeline or bloating MariaDB with chat content? | **Non-parametric feedback memory retriever.** A new `_FeedbackMemoryRetriever` Haystack component runs as a **3rd retriever** alongside vector + BM25, doing kNN over a new ES `feedback_v1` index of `(query_embedding, source_id, vote, reason, ts)` records. Results join via the existing `DocumentJoiner` with **weighted RRF** (`weights=[1.0, 1.0, CHAT_FEEDBACK_RRF_WEIGHT]`, default `0.5`). Feedback is **source-level only** (not chunk-level) — aligns with B36 hydrator's `document_id` correctness gate and survives chunk re-ingest. Per-source score = `wilson_lower_bound(likes, likes+dislikes, z=1.96) × 0.5^((now-ts_max)/CHAT_FEEDBACK_HALF_LIFE_DAYS)`, gated by `(likes+dislikes) ≥ CHAT_FEEDBACK_MIN_VOTES`. Behaves as instance-based supervised learning (memory-based collaborative filtering); no training infrastructure. **Latency budget:** kNN call bounded by `ES_QUERY_TIMEOUT_SECONDS` (§4.6.7, default 10s) like the existing two retrievers. **Concurrency:** P1 runs the three retrievers sequentially (matching the current §3.4 "P1 OPEN" note — retrievers sequential, P2 AsyncPipeline parallelises); worst-case latency = sum of three ES query budgets. **Chunk lookup after Wilson scoring:** single ES `terms` query on `chunks_v1.document_id` with all surviving `source_id`s in one round-trip (no N+1). Only active when `CHAT_JOIN_MODE=rrf` AND `CHAT_FEEDBACK_ENABLED=true`. | (a) Fine-tune embedder on feedback hard-negatives — strictly higher ROI long-term but needs training infra, A/B harness, and data accumulation; multi-week effort, defers MVP indefinitely. (b) Native ES RRF retriever with feedback as a sub-query — locks fusion into ES query DSL, breaks `CHAT_JOIN_MODE` topology dispatch (C6) and blocks future graph / reranker branch insertion at component level. (c) Per-doc `like_count` denormalised onto `chunks_v1` — query-independent popularity heuristic; cannot learn "this source is good FOR THIS QUERY TYPE"; no defence against cold-start or 1-vote noise. (d) Pairwise cross-encoder reranker training from feedback — same training-infra blocker as (a); revisit in P3. | §3.4 / §3.4.5 / §4.6.6 / §5.1 / §5.4 / T-FB.1–T-FB.12 |
| **B51** | 2026-05-16 | Retrieval | Where to persist the `(query, shown_sources)` snapshot that feedback semantically references — eager-write a `chat_traces` row on every `/chat` call (99% dead weight given <1% feedback rate), or lazy-write only when feedback arrives? | **Lazy write — no DB write on `/chat` path.** `/chat` response carries `request_id` (UUIDv7) + `feedback_token = HMAC(FEEDBACK_HMAC_SECRET, canonical_json({request_id, user_id, sources_hash, ts}))` where `sources_hash = sha256(json([s.source_id for s in sources]))`. `POST /feedback/v1` body echoes `{request_id, feedback_token, query_text, shown_source_ids, source_id, vote, reason?}`. Server verifies HMAC against `sha256(shown_source_ids)` (binds the shown set), checks `source_id ∈ shown_source_ids`, re-embeds `query_text` once per feedback event (`query_text` is NOT signed — trusted to re-embed without cross-event consequences), dual-writes **MariaDB `feedback` first, then ES `feedback_v1`** (matches `documents`→`chunks_v1` ordering, B36 invariant: MariaDB is SoT). ES write failure logs `event=feedback.es_write_failed` and increments `ragent_feedback_es_write_failed_total`; MariaDB row remains the truth and an offline replay job (P2) can re-derive `feedback_v1`. Token TTL = 7 days (`ts` in HMAC payload, validated server-side); past 7d → 410 `FEEDBACK_TOKEN_EXPIRED`. Payload size bounded by `len(sources) ≤ RETRIEVAL_TOP_K` (default 20). No `chat_traces` table needed — HMAC fully integrity-guards the client-carried snapshot. | (a) Eager-write `chat_traces` on every `/chat` — 99% rows dead weight at <1% feedback rate; MariaDB bloat without observable benefit; conflicts with project policy "text/content goes to ES, MariaDB stores meta only" (§5.1). (b) Ephemeral snapshot in Redis with TTL=24h, promoted on feedback arrival — adds Redis as load-bearing for correctness; cache eviction = silent data loss with no audit trail. (c) Trust client snapshot without HMAC — single authenticated user could poison `feedback_v1` by claiming arbitrary `shown_source_ids`; HMAC adds one ms of CPU and closes the gap. (d) Token without expiry — replay-attack window unbounded; 7d also matches the analytical-value cutoff (feedback older than a week rarely drives action). (e) Two-phase commit / transactional outbox MariaDB↔ES — adds outbox table + sweeper; over-engineered for a write where ES failure is recoverable from MariaDB. | §3.4 / §3.4.5 / §4.6.6 / §5.1 / `routers/feedback.py` / `routers/chat.py` / `utility/feedback_token.py` |
| **B52** | 2026-05-16 | Retrieval | `POST /feedback/v1` accepts a `reason?` field; free-text comment or closed enum? Which values? | **Closed enum, 6 values, frozen Day 1:** `irrelevant \| hallucinated \| outdated \| incomplete \| wrong_citation \| other`. Maps 1:1 to the four RAG failure layers (retrieval / grounding / index-freshness / coverage) plus citation and a catch-all. **New enum values require a new B-row** (append-only, never re-edit). No free-text `comment` field in MVP — `other` is the escape hatch; PII-scrubbing surface deferred until comment is actually shipped. Each reason routes to a different downstream behaviour: `irrelevant` → query-conditional down-rank (P2); `outdated` → re-ingest trigger (manual P1, automated P3); `hallucinated` / `wrong_citation` → generation-layer signals, retrieval unchanged; `incomplete` → query-expansion hint (P3); `other` → analytics only. | (a) Free-text `reason` — defeats aggregation; cannot dashboard or filter; PII-scrubbing burden from day 1. (b) Loose enum without B-row freeze — taxonomy drift breaks historical analytics joins; one enum-value rename invalidates months of training data. (c) Binary "this is bad" tag, no reason — collapses all failure modes into one signal; same low-resolution problem as no-reason dislike (forces all remediation to be "blanket model retrain"). (d) Defer reason collection to P2 — collecting structured reason from day 1 is cheap; retrofitting taxonomies onto historical raw votes is impossible. | §3.4.5 / §4.1.2 / `schemas/feedback.py` / `_FeedbackMemoryRetriever` (reason-conditional filter, P2) |
| **B53** | 2026-05-16 | Retrieval | Which feedback-system capabilities ship in P1 MVP vs deferred? Risk of over-engineering before observing real feedback distribution. | **P1 MVP ships only the closed-loop minimum:** HMAC token, `POST /feedback/v1`, dual-write, kNN retriever with Wilson + time-decay + min-votes gate, weighted RRF fusion at `CHAT_FEEDBACK_RRF_WEIGHT=0.5`, default `CHAT_FEEDBACK_ENABLED=false` (ship dark). **Deferred to P2+:** (1) Inverse Propensity Score (IPS) reweighting for position-bias debiasing — `position_shown` is recorded into MariaDB `feedback` from day 1 (zero cost) but not consumed; (2) Exploration / ε-greedy reservation of top-K slots for non-boosted candidates; (3) Reason-driven filter blacklisting (e.g. `irrelevant`-clustered sources auto-quarantined); (4) Automated `outdated` → re-ingest trigger; (5) Fine-tune embedder / reranker from accumulated `feedback` rows; (6) **Retention** — `feedback` table and `feedback_v1` index are append-only with no TTL in P1; query-side `ts > now - 90d` filter bounds retrieval-time impact but storage grows linearly with feedback events (estimated <1% chat rate ⇒ tolerable for P1 volumes); a reconciler-driven sweep keyed on retention window deferred until first P2 ops review; (7) MariaDB↔ES parity reconciler for `feedback` ↔ `feedback_v1` (analogue of B36 for chunks) — MVP relies on offline replay from MariaDB SoT to backfill ES on rare write failures. Re-evaluate each deferral after ≥ 3 months of dark-mode write-only data, or when online enable shows positive A/B lift plateau. | (a) Ship IPS from day 1 — adds click-model estimation + position-aware weighting; risk of premature complexity before observing actual position-bias magnitude; can be applied offline later from raw `feedback` table without backfill. (b) Skip Wilson, use raw `likes − α·dislikes` — small-sample noise dominates (single user voting twice swings ranking); Wilson is a one-line utility with strictly better behaviour. (c) Default `CHAT_FEEDBACK_ENABLED=true` — risks unexpected retrieval changes for current users; ship dark, observe write volume + dashboard reason distribution first. (d) Cut reason collection from MVP — schema cost is one nullable column; collecting from day 1 enables all reason-driven P2 features without backfill. | §3.4.5 / §4.6.6 / `_FeedbackMemoryRetriever` / T-FB.1–T-FB.12 / future plan rows |
