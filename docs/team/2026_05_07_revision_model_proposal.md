# Revision Model Proposal — Document Identity, Active Pointer, Multi-Embedding Coexistence

**Status:** DESIGN — not yet implemented. Awaiting team review.
**Date:** 2026-05-07
**Branch:** `claude/review-source-id-handling-dq6nj`
**Supersedes:** none (extends the supersede design in `docs/00_spec.md` §3.1).

## 1. Motivation

Two distinct problems share the same shape:

1. **Re-ingest of the same `(source_id, source_app)`** must replace the previous content end-to-end. Today the `documents` table carries everything; eventual uniqueness via supersede leaves orphan chunks in Elasticsearch (see review on this branch, fixed in this PR for the immediate cascade) and the model still entangles "logical document" with "one physical ingest".
2. **Embedding-model migration** (e.g. `bge-m3` → next-gen embedder). During the rollout, both the old and new embedding indexes must coexist and queries must be served by whichever revision matches the **query embedding's** model. Today `chunks_v1` carries chunks with no notion of which embedding model produced them at the document level, so a multi-model rollout is unsafe.

A single concept — **revision** — addresses both: each ingest produces a new revision; the document points to the **active revision**; old revisions linger until either superseded or eligible for sweep; embedding-model swaps are just "ingest a revision under a new model".

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
- DROP `source_workspace` (out of scope for this branch per session decision; tracked separately).
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

1. **Audit-window for non-active revisions** — duration, sweep cadence, whether we keep MinIO objects too. Default proposal: 24h, swept by reconciler.
2. **Embedding dim drift** — different revisions can have different `embedding_dim`; ES index `chunks_v1` is single-dim. Migration probably needs a second index `chunks_v2` for the new model and a routing rule by model. Decide before code.
3. **Active-pointer write contention** — atomic `UPDATE … WHERE active_revision_id IN (…)` is single-row, fine. Reconciler must not race the worker; standard `SELECT … FOR UPDATE SKIP LOCKED` pattern.
4. **API breakage** — `documents.status` no longer exists; `GET /ingest/{id}` shape changes (status moves under `revisions[]` or `active_revision`). Need a migration story for clients.
5. **Backfill cost** — adding `revision_id` to all existing chunks is an ES update-by-query; size-dependent.

## 7. Decision needed

The team should decide:
- **D1**: Adopt revision model in P1 vs. defer to P2. (Recommendation: P2 — current branch fixes the immediate cascade bug; revision model is a multi-day track.)
- **D2**: Single-model bulk-flip vs. per-document mixed-model retrieval during embedding migration. (Recommendation: bulk-flip — simpler, sufficient for first migration.)
- **D3**: Whether `source_workspace` removal (this branch) lands before or after revision migration. (Recommendation: before — independent, mechanical.)

If approved, follow-up work:
- Add Decision Log row in `docs/00_spec.md` §7.
- Spec rewrite for §3.1 / §3.4 / §6.
- New entries in `docs/00_plan.md` for the implementation track.
