# Revision Model Proposal — Document Identity, Active Pointer, Multi-Embedding Coexistence

**Status:** DESIGN — not yet implemented. Awaiting team review.
**Date:** 2026-05-07
**Branch:** `claude/review-source-id-handling-dq6nj`
**Supersedes:** none (extends the supersede design in `docs/00_spec.md` §3.1).

## 1. Motivation

### 1.1 Primary driver — eliminate the user-visible inconsistency window

The current model couples **logical visibility** to **physical row count**: a row is visible to retrieval iff `status = 'READY'`. During re-ingest of the same `(source_id, source_app)`, two rows are READY at the same time between "new doc finishes" and "reconciler runs supersede". For that window (seconds, but unbounded if the reconciler stalls) **users see old + new content interleaved in chat results** — same logical document, two physical sets of chunks both ranked. This is not a theoretical bug; it surfaces every time content updates.

Splitting the document into `documents` + `document_revisions` with an `active_revision_id` pointer separates two concerns the current schema conflates:

| Concern | Current model | Revision model |
|---|---|---|
| New content visible to users | Immediate (status flips READY) | Immediate (atomic pointer flip in same tx) |
| Old content stops being retrieved | **Wait for reconciler + supersede** (race window) | Immediate (retrieval filters on `revision_id = active_revision_id`) |
| Old content removed from disk | Coupled to "stops being retrieved" | Decoupled; sweep happens later per audit window |
| What users see during the window | **Old + new mixed** | Only new |

The win is moving "switchover" from `eventual consistency via reconciler tick` to `atomic conditional UPDATE inside the worker transaction`. Reconciler stays — but only for physical reclaim, not for correctness.

### 1.2 Secondary drivers

1. **ES-orphan safety net.** Even with the cascade fix on this branch (`supersede` now routes through `self.delete`), the design still relies on the supersede path running. Active-pointer routing means even a stalled supersede leaves the system **logically correct** — old chunks exist on disk but are never ranked because the filter excludes them.
2. **Embedding-model migration.** `bge-m3` → next-gen embedder needs both indexes to coexist with per-document routing of the query embedding. A revision row carries `embedding_model` + `embedding_dim`; the active pointer encodes "which model serves this doc right now". Migration becomes "stand up parallel index, reembed cohort, flip pointers". See §3.4.
3. **Rollback capability.** Within the audit window, rollback is a pointer flip back to a prior revision — no re-embed, no re-ingest. After the window, rollback is a re-ingest under the old model. The same machinery covers both.
4. **Audit / reproducibility.** `(document_id, revision_id, embedding_model, created_at)` lets us answer "what content + what model produced this answer last week" exactly.

## 2. Concept

```
documents                              document_revisions
─────────                              ──────────────────
id BIGINT AUTO_INCREMENT PK            id BIGINT AUTO_INCREMENT PK
document_id CHAR(26) UNIQUE  ←──────── document_id CHAR(26) (FK app-level)
source_id VARCHAR(128)                 revision_id CHAR(26) UNIQUE
source_app VARCHAR(64)                 embedding_model VARCHAR(64)
UNIQUE (source_id, source_app)         embedding_dim INT
active_revision_id CHAR(26) NULLABLE   status ENUM(PENDING,READY,FAILED,DELETING)
source_title VARCHAR(256)              object_key VARCHAR(...)
source_url VARCHAR(2048)               attempt INT, heartbeat_at, created_at, updated_at
created_at, updated_at                 INDEX (document_id, status, created_at)
                                       INDEX (status, heartbeat_at)
```

- **`documents`** = the logical thing. One row per `(source_id, source_app)`. Now safely UNIQUE — transient-duplicate role moves to revisions.
- **`document_revisions`** = one ingest attempt with its own lifecycle, content, and embedding-model binding.
- **`active_revision_id`** = a column (not a separate table) that points to the revision currently authoritative for retrieval. NULL until the first revision reaches READY.
- **ES chunks** carry both `document_id` and `revision_id` in their metadata.

## 3. Lifecycle

### 3.1 Re-ingest of an existing `(source_id, source_app)`

1. `POST /ingest` with the same pair → upsert `documents` row (no-op if exists), insert a new `document_revisions` row in `PENDING`.
2. Worker pipeline runs against the revision (chunk → embed → index in ES with `revision_id` metadata).
3. On success: revision flips `PENDING → READY`, then **atomically** `UPDATE documents SET active_revision_id = :new_revision_id WHERE document_id = :doc_id AND (active_revision_id IS NULL OR active_revision_id IN (current, prior))`.
4. **Old revision** transitions `READY → SUPERSEDED` (new terminal state) and is enqueued for sweep: `fan_out_delete(revision_id)` to drop chunks; row retained for audit window (config'd, e.g. 24h) then physical delete.
5. On failure: revision goes `FAILED`; `active_revision_id` untouched; old revision stays authoritative — same zero-downtime guarantee as today's supersede.

**Properties carried over from current spec:**
- Latest-write-wins on `MAX(created_at)` of READY revisions.
- Out-of-order finish handled by the atomic flip's `WHERE` clause.
- Idempotent (re-running the flip is a no-op).
- Reconciler R3-equivalent: scan `documents` where multiple revisions are `READY` and the active pointer is stale.

### 3.2 Delete by `document_id`

1. `DELETE /ingest/{document_id}` claims the documents row, fans out delete on **all** revisions (active + non-active still in audit window), drops the documents row last.
2. Idempotent on missing.

### 3.3 Delete by `source_id`

Now well-defined: lookup `documents` by `(source_id, source_app)` (UNIQUE — single row), then run §3.2. New endpoint surface required:
`DELETE /ingest?source_id=…&source_app=…` → 204 / 404.

### 3.4 Embedding-model migration

1. Operator sets the rollout model (e.g. `bge-m3-v2`) and a percentage / cohort.
2. For each in-cohort document, kiq `ingest.reembed(document_id, embedding_model=v2)` creates a new revision with `embedding_model='bge-m3-v2'`, runs the pipeline, indexes to a parallel ES alias (or same index — chunks carry the model field).
3. **During rollout**, queries embed the question with **the model that matches `documents.active_revision_id`'s `embedding_model`**. Two ways to land this:
   - **Per-document**: chat retrieval embeds the question once per distinct active model in the candidate set. Costs more for mixed corpora.
   - **Bulk-flip**: rollout proceeds doc-by-doc but the active flip waits for the entire cohort, so at any moment only one model is authoritative. Simpler.
4. Once the cohort is complete, sweep all non-active revisions of the old model.

This is the structural reason for revisions: without them, "switch the embedding model" requires a full re-ingest with downtime or a parallel `documents` table.

## 4. Schema migration sketch

`documents` (existing):
- ADD `id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY` (drop existing PK on `document_id`, recreate as `UNIQUE KEY`).
- ADD `active_revision_id CHAR(26) NULL`.
- ADD `UNIQUE KEY uq_source_pair (source_id, source_app)` — **only safe after** the supersede backlog is drained and revisions own transient duplicates.
- `source_workspace` is renamed to `source_meta VARCHAR(1024)` on this branch (free-format); revision model inherits the new name.
- KEEP `idx_source_app_id_status_created` until reconciler stops querying it.
- The status / object_key / attempt / heartbeat / per-ingest columns on `documents` MOVE to `document_revisions`.

`document_revisions` (new):
- Columns as in §2.
- Migration data path: for each existing `documents` row, create one `document_revisions` row carrying its current `(status, object_key, attempt, heartbeat_at, created_at, updated_at)`; set `documents.active_revision_id = revisions.revision_id` if `status = READY`.

`chunks_v1` (ES):
- ADD field `revision_id` (keyword). Backfill from `document_id → active_revision_id` lookup at migration time.
- Retrieval filter switches from `document_id IN (...)` to `revision_id = active_revision_id`.

## 5. Code surfaces affected

- `src/ragent/repositories/document_repository.py` — split into `DocumentRepository` and `RevisionRepository`. Most current methods (status transitions, heartbeat, supersede helpers) move to revisions.
- `src/ragent/services/ingest_service.py` — `create` / `delete` / `supersede` (renamed → `flip_active`).
- `src/ragent/workers/ingest.py` — pipeline keyed by `revision_id`; new `ingest.reembed` task.
- `src/ragent/reconciler.py` — multi-READY repair becomes "stale active pointer repair".
- `src/ragent/plugins/vector.py` — index docs by `revision_id`; delete-by-revision contract.
- `src/ragent/pipelines/chat.py` — retrieval filter on `revision_id = active_revision_id`; per-revision embedding model lookup for query embedding.
- Spec §3.1 (rewrite supersede sub-section), §3.4 (retrieval filter), §6 schema, S17–S26 scenarios, Decision Log row.
- Tests across `tests/{unit,integration,e2e}/` for ingest, supersede→flip, reconciler, retrieval, chat hydration.

## 6. Risks / open questions

### 6.1 Layered audit window (chosen)

A single global `AUDIT_WINDOW` is wrong — different revision sources have different retention value. Add a column `retention_class ENUM('migration','reingest','none')` on `document_revisions`; sweep job applies a per-class window:

| `retention_class` | Default window | Rationale |
|---|---|---|
| `migration` | **∞** (or 90 d if storage-bound) | Embedding-model rollouts are rare, operator-driven, and rollback is a pointer flip — only possible while old chunks still exist. |
| `reingest` | **24 h** | Repeated content updates are frequent and rarely need historical reconstruction. |
| `none` | **0** (immediate sweep on demotion) | Reserved for explicit "do not retain" flows (e.g. PII redaction reingest). |

**Operational guards** required when any window is set to ∞ (without these the rule is irresponsible):

1. **Explicit `DELETE /ingest/{id}` always cascades all revisions**, regardless of retention class. Compliance ("right to be forgotten") cannot be blocked by retention policy.
2. **Capacity alarms** on ES disk + MinIO bucket size; sweep falls back to "drop oldest non-active revisions first" past a threshold.
3. **Per-source override**: operator can shorten the window on a hot/large source without touching global config.
4. **Backup exclusion**: snapshots back up only `active_revision` chunks by default; non-active revisions are not part of the disaster-recovery surface.

### 6.2 Other open questions

1. **Embedding dim drift** — different revisions can have different `embedding_dim`; ES `dense_vector` is dim-locked per field. Plan: parallel indexes `chunks_v1` (old dim) + `chunks_v2` (new dim) keyed by `embedding_model`; retrieval routes by active revision's model. Decided before code (B33 — per-document active-model routing).
2. **Active-pointer write contention** — atomic `UPDATE … WHERE active_revision.created_at < new_revision.created_at` is single-row, fine. Reconciler must not race the worker; standard `SELECT … FOR UPDATE SKIP LOCKED` pattern.
3. **API breakage** — `documents.status` no longer exists; `GET /ingest/{id}` shape changes (status moves under `active_revision` or a `revisions[]` array). Need a client-migration story.
4. **Backfill cost** — adding `revision_id` to all existing chunks is an ES update-by-query; size-dependent. Acceptable as a one-time migration cost.

## 7. Decisions (locked 2026-05-07)

These are recorded in `docs/00_spec.md` §7 as B32 / B33 / B34.

- **D1 (B32)**: **Revision model deferred to Phase 2.** This branch only fixes the supersede cascade and adds the survivor guard; no schema split. Phase 1 stays focused on closing the existing bugs. Revision model is its own multi-day track on a new branch with its own plan.md entries.
- **D2 (B33)**: **Per-document active-model routing** during embedding migration. Query path looks up each candidate document's active revision, groups by `embedding_model`, and embeds the question once per distinct model in the candidate set (typically 1, at most 2 during rollout). Enables canary / cohort migration. Bulk-flip (single global model) rejected — no canary path.
- **D3 (B34)**: **Layered audit window for revision sweep** — `retention_class ∈ {migration, reingest, none}`, defaults `migration=∞`, `reingest=24h`, `none=0`. Subject to the four operational guards in §6.1. Single global window rejected — too aggressive for migration rollback, too lax for routine reingests.

### Follow-up work (when revision model implementation begins on a new branch)

- Spec rewrite for §3.1 / §3.4 / §6 to introduce revision concept.
- New entries in `docs/00_plan.md` for the implementation track.
- New `00_journal.md` entries on the Architecture / Spec domains capturing why the split matters (anchor: 1.1 above).
