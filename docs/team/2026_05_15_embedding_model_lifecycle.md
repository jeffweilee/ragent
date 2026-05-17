# Embedding Model Lifecycle — Zero-Downtime Migration via Multi-Vector Single Index

> **Status:** Design (Plan-only, 2026-05-15) · **Branch:** `claude/design-embedding-model-switch-5N3Uh`
> **Supersedes / refines:** B33 / B34 / B35 in `docs/00_spec.md`. This document is the operative
> contract for Phase 1.5 embedding-model switch; B33's per-document active-model routing remains
> the long-term design for true canary cohorts and is **not** built today.

---

## 1. Goals

A future embedding-model swap must satisfy **all** of:

- **Zero downtime** — no service interruption during the swap.
- **Zero restart** — no process reload; all changes are data-only (SQL row + ES mapping).
- **Painless rollback** — single SQL `UPDATE` reverts the swap; no data movement.
- **Rollback-window write safety** — document updates that land during the rollback window
  remain consistent across both old and new models, so rollback after a `READY` ingest still
  serves correct results.
- **Operator simplicity** — five admin APIs, one state machine, one settings table. Cheat-sheet
  fits on one page.

---

## 2. Core Idea

**Single ES index** (`chunks_v1`) carries **multiple per-model vector fields** side-by-side
during migration. Field name pattern: `embedding_<model_normalized>_<dim>`.

- Steady state (IDLE): one vector field is populated; ingest writes one, query reads one.
- Migration state (CANDIDATE / CUTOVER): two vector fields are populated via **dual-write**;
  query reads from one (selected by `embedding.read` setting).
- After commit, the previous field enters a **retired** list; the reconciler clears its values
  by `_update_by_query` over a sweep window.

**Why this design beats alias-flip:** alias flip leaves new docs ingested between alias-flip
and rollback in the new index only — rolling back loses them. Multi-vector dual-write
keeps **both** vectors current on **every** upsert, so rollback is truly stateless.

---

## 3. State Machine

```
        ┌─────────┐
        │  IDLE   │  stable=A, candidate=null, read=stable
        └────┬────┘
             │ promote(B)
             ▼
      ┌──────────────┐
      │  CANDIDATE   │  stable=A, candidate=B, read=stable
      └──┬────────┬──┘
   abort │        │ cutover
         │        ▼
         │  ┌──────────────┐
         │  │   CUTOVER    │  stable=A, candidate=B, read=candidate
         │  └──┬────────┬──┘
         │     │        │ commit
         │ rollback     │
         │     │        ▼
         │     │   ┌──────────┐
         │     │   │  IDLE'   │  stable=B (promoted), candidate=null
         │     ▼   └──────────┘
         │  (back to CANDIDATE)
         ▼
      ┌─────────┐
      │  IDLE   │  unchanged stable, candidate=null
      └─────────┘
```

### Allowed transitions

| From       | Transition | To         | Notes                                                                 |
|------------|------------|------------|-----------------------------------------------------------------------|
| IDLE       | `promote`  | CANDIDATE  | adds candidate; opens dual-write                                      |
| CANDIDATE  | `cutover`  | CUTOVER    | flips `embedding.read` to candidate                                   |
| CANDIDATE  | `abort`    | IDLE       | drops candidate; closes dual-write; retires candidate field           |
| CUTOVER    | `rollback` | CANDIDATE  | flips `embedding.read` back to stable; dual-write **stays open**      |
| CUTOVER    | `commit`   | IDLE       | promotes candidate → stable; retires old stable field                 |

### Rejected transitions (raise `IllegalEmbeddingTransition`)

- `IDLE → cutover`, `IDLE → rollback`, `IDLE → commit`, `IDLE → abort`
- `CANDIDATE → commit` (must cutover first)
- `CUTOVER → promote`, `CUTOVER → abort` (must rollback first)
- Any `→ promote` when `candidate` is already non-null

**Invariant:** at most one candidate at any time. `abort` is the only legal exit from
CANDIDATE that does not advance state; `commit` is the only legal exit from CUTOVER that
changes `stable`.

---

## 4. Settings Schema

New table `system_settings` (single source of truth, hot-path readable, polled by App via
TTL cache):

```sql
CREATE TABLE system_settings (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  setting_key VARCHAR(64) NOT NULL,
  setting_value JSON NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_setting_key (setting_key)
);
```

### Rows used by the lifecycle (exactly four)

| `setting_key`         | `setting_value` (JSON)                                                                  | Mutated by                            |
|-----------------------|-----------------------------------------------------------------------------------------|---------------------------------------|
| `embedding.stable`    | `{"name":"bge-m3","dim":1024,"api_url":"...","model_arg":"bge-m3","field":"embedding_bgem3_1024"}` | `promote` (init), `commit`            |
| `embedding.candidate` | same shape as above, **or** `null`                                                      | `promote`, `commit`, `abort`          |
| `embedding.read`      | `"stable"` or `"candidate"`                                                             | `cutover`, `rollback`, `commit`       |
| `embedding.retired`   | `[{"name":..., "dim":..., "field":..., "retired_at":"<ISO>", "cleanup_done":false}, ...]` | `commit`, `abort`, reconciler         |

### Initial state (bootstrap migration)

```json
embedding.stable    = {"name":"bge-m3","dim":1024,"api_url":"<EMBEDDING_API_URL>","model_arg":"bge-m3","field":"embedding_bgem3_1024"}
embedding.candidate = null
embedding.read      = "stable"
embedding.retired   = []
```

The current bootstrap of `EmbeddingClient` (hardcoded `bge-m3` / 1024) becomes the
initial `stable` row. App reads model identity from settings, not env.

### Field-name normalization

`<model_normalized>` = `lower(strip_non_alnum(name))`. Examples:
- `bge-m3` → `bgem3`
- `text-embedding-3-large` → `textembedding3large`
- `BGE-M3-v2` → `bgem3v2`

ES field name: `embedding_<normalized>_<dim>` (e.g. `embedding_bgem3_1024`).
Dim range guard: `1 ≤ dim ≤ 4096` (ES `dense_vector` limit).

---

## 5. Admin API

All five endpoints live under router `/embedding/v1`. Each performs validation,
state-machine assertion, side effects, audit log, and Prometheus event in that order.

### 5.1 `POST /embedding/v1/promote`

Open a migration round. Adds the candidate field to ES mapping and enables dual-write.

**Request body:**
```json
{
  "name": "bge-m3-v2",
  "dim": 768,
  "api_url": "http://embedder-v2.internal/text_embedding",
  "model_arg": "bge-m3-v2"
}
```

**Side effects (in order):**
1. State-machine guard: current state must be **IDLE**.
2. Validate `dim ∈ [1, 4096]`; compute `field = "embedding_<normalized>_<dim>"`.
3. Reject if `field` already exists in ES mapping (name collision with retired-but-uncleared).
4. `PUT chunks_v1/_mapping` adding the new dense_vector field. (ES online operation.)
5. `UPDATE system_settings SET setting_value = <candidate JSON> WHERE setting_key='embedding.candidate'`.
6. Audit log row + Prometheus `embedding_lifecycle_total{action="promote",to=<name>}`.
7. Returns `200 OK` with `{state:"CANDIDATE", candidate:{...}, promoted_at:<ISO>}`.

After this call, the next App-cache refresh (≤ TTL) makes ingest workers write both
the stable and candidate vector fields. Operator's next step: **backfill** existing
documents by re-POSTing `/ingest/v1` with same `(source_id, source_app)`, which exercises
the existing supersede path with dual-write enabled.

### 5.2 `POST /embedding/v1/cutover`

Switch reads from stable to candidate. Subject to preflight gates (see §6).

**Request body:** `{}` (or `{"force": true}` to override soft gates — hard gates not overridable)

**Side effects:**
1. State-machine guard: current state must be **CANDIDATE**.
2. Run preflight; on any hard-gate failure return `409 Conflict` + `preflight` report.
3. `UPDATE system_settings SET setting_value='"candidate"' WHERE setting_key='embedding.read'`.
4. Audit + Prometheus `embedding_lifecycle_total{action="cutover"}`.
5. Returns `{state:"CUTOVER", read:"candidate", cutover_at:<ISO>, effective_at:<cutover_at + cache_ttl>}`.

### 5.3 `POST /embedding/v1/rollback`

Revert reads to stable. Dual-write remains open.

**Side effects:**
1. State-machine guard: current state must be **CUTOVER**.
2. `UPDATE system_settings SET setting_value='"stable"' WHERE setting_key='embedding.read'`.
3. Audit + Prometheus `embedding_lifecycle_total{action="rollback"}`.
4. Returns `{state:"CANDIDATE", read:"stable", rolled_back_at:<ISO>}`.

### 5.4 `POST /embedding/v1/commit`

Promote candidate to stable. Old stable enters retired list.

**Preflight (soft gates):**
- `time_in_cutover ≥ COMMIT_MIN_HOURS` (default 24h) — prevents impulsive commit.
- No rollback recorded in last `COMMIT_MIN_HOURS`.

**Side effects:**
1. State-machine guard: current state must be **CUTOVER**.
2. Run preflight; on any failure return `409 Conflict` unless `force=true`.
3. In one transaction:
   - Append the old `stable` to `embedding.retired` with `cleanup_done=false`.
   - Set `stable` ← `candidate`, `candidate` ← `null`, `read` ← `"stable"`.
4. Audit + Prometheus `embedding_lifecycle_total{action="commit",to=<new_stable_name>}`.
5. Returns `{state:"IDLE", stable:{...new...}, committed_at:<ISO>}`.

### 5.5 `POST /embedding/v1/abort`

Drop candidate without committing. Used after rollback or before cutover.

**Side effects:**
1. State-machine guard: current state must be **CANDIDATE** (i.e. not CUTOVER — must
   rollback first to ensure no readers are still on candidate).
2. In one transaction:
   - Append the current `candidate` to `embedding.retired` with `cleanup_done=false`.
   - Set `candidate` ← `null`.
3. Audit + Prometheus `embedding_lifecycle_total{action="abort",candidate=<name>}`.
4. Returns `{state:"IDLE", aborted:<candidate_name>, aborted_at:<ISO>}`.

### 5.6 `GET /embedding/v1/state` (read-only)

Returns the full settings snapshot plus computed state. Used for diagnostics and CI scripts.

```json
{
  "state": "CUTOVER",
  "stable":    {"name":"bge-m3","dim":1024,"field":"embedding_bgem3_1024","api_url":"..."},
  "candidate": {"name":"bge-m3-v2","dim":768,"field":"embedding_bgem3v2_768","api_url":"..."},
  "read":      "candidate",
  "retired":   [],
  "cache_ttl_seconds": 10,
  "preflight_url": "/embedding/v1/cutover/preflight"
}
```

### 5.7 `GET /embedding/v1/cutover/preflight` (read-only)

Runs cutover preflight without taking action. Returns pass/fail per gate. See §6.

---

## 6. Cutover Preflight

### Hard gates (always enforced; cutover refuses with `409 Conflict`)

| Gate                     | Check                                                                                 |
|--------------------------|---------------------------------------------------------------------------------------|
| `state_is_candidate`     | `state == CANDIDATE`                                                                  |
| `field_dim_matches`      | `candidate.dim == ES mapping[candidate.field].dims`                                   |
| `candidate_coverage`     | `count(chunks where exists(candidate.field)) / count(chunks) ≥ 0.99`                  |
| `dual_write_warmup`      | `(now - candidate.promoted_at) ≥ 2 × cache_ttl_seconds` (App cache propagation done)  |

### Soft gates (warn; bypassable with `force=true`)

| Gate                       | Check                                                                                |
|----------------------------|--------------------------------------------------------------------------------------|
| `candidate_embed_health`   | Prometheus query: candidate model embed success rate ≥ 99% over last 10 min          |
| `recent_benchmark_passed`  | A benchmark POST recorded in last 7 days with `recall_at_10 ≥ baseline × 0.95`       |

Preflight response shape:

```json
{
  "pass": false,
  "gates": [
    {"name":"candidate_coverage", "level":"hard", "pass":false,
     "detail":{"covered":98765, "total":100000, "ratio":0.98765, "threshold":0.99}},
    {"name":"dual_write_warmup", "level":"hard", "pass":true,
     "detail":{"elapsed_seconds":42, "required_seconds":20}}
  ]
}
```

---

## 7. Timelines (canonical scenarios)

### 7.1 Happy path (A → B, commit)

```
T0    state=IDLE          stable=A    candidate=null   read=stable
T1    POST /promote {B}
      state=CANDIDATE     stable=A    candidate=B      read=stable
      ─ ES: chunks_v1 mapping gains embedding_bgem3v2_768
      ─ Ingest: each new doc embeds with A AND B, writes both fields

T2    Operator backfills: re-POST /ingest/v1 for each pre-existing doc
      Supersede produces new chunks with both fields filled

T3    Operator runs benchmark; records pass to settings
T4    POST /cutover
      state=CUTOVER       stable=A    candidate=B      read=candidate
      ─ Preflight green; one SQL row updated
      ─ Cache TTL elapses (~10s) → all queries embed with B and kNN on embedding_bgem3v2_768

T5    Observation window (≥ COMMIT_MIN_HOURS, default 24h)
T6    POST /commit
      state=IDLE          stable=B    candidate=null   read=stable
      ─ A's field added to retired list
      ─ Reconciler begins _update_by_query to unset embedding_bgem3_1024 (batched, 10k at a time)
```

### 7.2 Rollback path (A → B → rollback → abort)

```
T0–T4 same as 7.1
T5    Observation: B's recall drops; precision unstable
T6    POST /rollback
      state=CANDIDATE     stable=A    candidate=B      read=stable
      ─ Single SQL row updated
      ─ Cache TTL elapses → queries return to A
      ─ Dual-write still on: any subsequent /ingest writes BOTH A and B fields
        → if the operator later decides to retry cutover, B is still current

T7    Operator decides to abandon B
      POST /abort
      state=IDLE          stable=A    candidate=null   read=stable
      ─ B's field enters retired list
      ─ Reconciler clears embedding_bgem3v2_768 values; mapping field remains (ES limitation)
```

### 7.3 Pre-cutover abandonment (promote → abort)

```
T0    state=IDLE          stable=A    candidate=null
T1    POST /promote {B}
      state=CANDIDATE
T2    Backfill underway; operator notices B's embed API is flaky (50% success)
T3    POST /abort
      state=IDLE          stable=A    candidate=null
      ─ Pre-existing docs that received B vectors during backfill: B field retained but stranded
      ─ Reconciler clears embedding_bgem3v2_768 (same path as 7.2 T7)
```

### 7.4 Update inside rollback window (correctness of safety claim)

```
T0–T4 same as 7.1 (now in CUTOVER, read=candidate=B)
T5    User POSTs /ingest/v1 with same (source_id, source_app) on existing doc D
      Worker pipeline runs:
        ─ Reads embedding.write derived from settings: ["embedding_bgem3_1024","embedding_bgem3v2_768"]
        ─ Embeds chunk text with BOTH A and B
        ─ Writes chunk with both fields populated
        ─ Supersede demotes prior revision
T6    POST /rollback
      state=CANDIDATE     read=stable
      ─ Query of D now uses A's vector — and D's chunks have the freshly-rewritten A vector
        because step T5 dual-wrote both fields. No staleness.
```

This T5 → T6 sequence is the **rollback-window write safety** invariant: as long as
dual-write is on (i.e. `candidate` is non-null), every doc update keeps both fields
current. Rollback at any time during this window is safe.

---

## 8. Pipeline & Cache Contracts

### 8.1 ActiveModelRegistry (App-side cache)

Single composition-root singleton replacing the hardcoded `bge-m3` constant. Responsibilities:

- On boot: read settings; subscribe to refresh.
- Background asyncio task: refresh every `EMBEDDING_REGISTRY_TTL_SECONDS` (default 10).
- Expose three accessors:
  - `read_model() → ModelConfig` — used by `_QueryEmbedder` in chat pipeline.
  - `write_models() → list[ModelConfig]` — derived: `[stable, candidate]` if candidate
    non-null else `[stable]`. Used by ingest pipeline.
  - `state() → EmbeddingLifecycleState` — for /embedding/v1/state.

### 8.2 Dual-write in ingest pipeline

`DocumentEmbedder` (Haystack component) takes the registry, calls
`registry.write_models()`, and for each `ModelConfig` invokes the corresponding
`EmbeddingClient` instance. Output chunk has `embedding_<m1>_<d1>`, `embedding_<m2>_<d2>`
fields populated.

ES bulk write `DocumentWriter` includes all populated vector fields; absent fields
are simply not written (ES `dense_vector` skips missing — a chunk with only the stable
field is queryable on stable only, transparent to other fields).

### 8.3 Query path

`_QueryEmbedder` calls `registry.read_model()`, embeds with that one model, returns
`(embedding, field_name)`. Downstream retriever uses `field_name` as the kNN field —
no global field-name constant.

### 8.4 Failure modes

| Failure                                              | Behavior                                                         |
|------------------------------------------------------|------------------------------------------------------------------|
| Candidate embedder unreachable mid-dual-write        | Ingest fails this doc; reconciler retries (existing path)        |
| Settings cache refresh fails                         | Use last good cache; emit `event=embedding.cache.stale` warning  |
| State transition raises (e.g. concurrent admin call) | Return `409`; transaction-rollback the SQL                       |
| Cache TTL has not propagated yet at cutover          | `dual_write_warmup` gate enforces ≥ 2 × TTL before allowing cutover |

---

## 9. Retired Field Cleanup

ES does not allow dropping a field from a mapping (only reindex can). So retired fields
remain in mapping forever (or until a once-a-year reindex). Their **values**, however,
can be removed by `_update_by_query`:

```
POST chunks_v1/_update_by_query?conflicts=proceed&slices=auto
{
  "script": { "source": "ctx._source.remove('embedding_bgem3v2_768')" },
  "query":  { "exists": { "field": "embedding_bgem3v2_768" } }
}
```

A reconciler arm (`reconciler.retired_embedding_sweep`) polls `embedding.retired`, runs
the above against each entry with `cleanup_done=false`, and patches the entry on completion.
Runs at most once per minute; rate-limited to one slice at a time to avoid ES write storm.

---

## 10. Compatibility Notes

- **B33** (per-document active-model routing): **superseded by this design for
  embedding-model migration.** The original B33 plan (two parallel ES indexes keyed by
  `active_revision.embedding_model`) is no longer the path forward. If a per-document
  cohort canary is ever needed (e.g. AB testing two models on disjoint document subsets
  in production traffic), it can be layered on top of this design's mapping by adding a
  per-doc field-name override in `_QueryEmbedder` — no schema rework required.
- **B34** (`retention_class` ENUM): **superseded by this design for embedding-field
  retention.** Retired-field cleanup is a single-class operation handled by
  `embedding.retired` + the `retired_embedding_sweep` reconciler arm (§9). The original
  three-tier ENUM is no longer needed for embedding migration. If non-embedding revision
  retention is ever required, the B34 design remains the reference proposal for that
  separate concern.
- **B35** (production reindex via Alembic + controlled mapping update): preserved. Boot
  auto-init refuses to mutate existing mappings; the only mapping changes here go through
  `POST /embedding/v1/promote`, which is an explicit operator action.

---

## 11. Operator Cheat Sheet

```
Switch from current model to candidate B:
  1. POST /embedding/v1/promote   {name, dim, api_url, model_arg}
  2. Backfill: re-POST /ingest/v1 for each existing doc
  3. (optional) POST /embedding/v1/benchmark with recall_at_10 results
  4. POST /embedding/v1/cutover

If unhappy:
  5a. POST /embedding/v1/rollback
  6a. POST /embedding/v1/abort

If happy (after ≥ 24h observation):
  5b. POST /embedding/v1/commit
```

State at any moment: `GET /embedding/v1/state`.

Preflight check: `GET /embedding/v1/cutover/preflight`.

---

## 12. Out of Scope (deferred)

- Per-document model routing (B33). Single-cohort migration only.
- Reindex automation to remove retired fields from mapping (manual annual op).
- Admin UI / dashboards. CLI / `curl` for now.
- Multi-tenant per-tenant model selection. One model per cluster.
- Live A/B traffic split. Use offline benchmark before cutover.
- Embedding model versioning beyond a single `name` string (no semver of model weights).
