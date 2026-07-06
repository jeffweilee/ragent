# 4.1 Endpoints

> Linked from [`docs/00_spec.md` §4. Inventories](../00_spec.md#4-inventories).

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
| POST   | `/ingest/v1`               | `X-User-Id` | **JSON** (v2, see override above) | `202 { document_id }` |
| GET    | `/ingest/v1/{id}`          | `X-User-Id` | — | `200 { status, attempt, updated_at }` |
| GET    | `/ingest/v1?after=&limit=&source_id=&source_app=` | `X-User-Id` | — | `200 { items, next_cursor }` (limit ≤ 100; ordered `document_id DESC`; `source_id`/`source_app` are optional exact-match filters) |
| DELETE | `/ingest/v1/{id}`          | `X-User-Id` | — | `204` idempotent |
| POST   | `/ingest/v1/{id}/rerun`    | `X-User-Id` | — | `202 { document_id }` — manual re-dispatch of `ingest.pipeline` for non-READY/non-DELETING rows; `404 INGEST_NOT_FOUND` / `409 INGEST_NOT_RERUNNABLE` per S41. |
| POST   | `/ingest/v1/upload`        | `X-User-Id` | `multipart/form-data` (server stages to `__default__` MinIO; identical downstream to inline) | `202 { document_id }` |
| POST   | `/ops/v1/retry`            | `X-User-Id` | `{ statuses[], dry_run?, source_app?, source_id?, created_after?, limit? }` — batch force-retry stuck ingest documents, bypassing the reconciler's redispatch window | `200 { dry_run, counts, queued, skipped }` — see [`docs/00_API.md §Batch force-retry`](../00_API.md#post-opsv1retry--batch-force-retry-stuck-documents) |
| POST   | `/retrieve/v1`             | `X-User-Id` | §3.4.4 schema (`query` required; rest default) | `200 { chunks[] }` per §3.4.4 |
| POST   | `/chat/v1`                 | `X-User-Id` | §3.4.1 schema (`messages` required; rest default) | `200 application/json` per §3.4.2 |
| POST   | `/chat/v1/stream`          | `X-User-Id` | §3.4.1 schema | `text/event-stream` per §3.4.3 (`data: {type:delta\|done\|error}`) |
| POST   | `/feedback/v1`             | `X-User-Id` | §3.4.5 schema | `204` on success; `401`/`410`/`422` `application/problem+json` per §3.4.5. |
| POST   | `/mcp/v1`               | `<RAGENT_USER_ID_HEADER>` (P1) / `<RAGENT_JWT_HEADER>` (P2) | JSON-RPC 2.0 envelope per §3.8 | `200` with JSON-RPC response envelope; `204` for `notifications/*`. Auth failure (401) returns `application/problem+json` per §3.8.1 (transport-layer). |
| GET    | `/livez`                | none        | — | `200 {"status":"ok"}` — process up; no dependency probes |
| GET    | `/startupz`             | none        | — | `200 {"status":"ok"}` once all probes have been green at least once since boot; `503` until then. Latch: flips permanently to ready after first green `/readyz` sweep. |
| GET    | `/readyz`               | none        | — | `200` if all dep probes pass; else `503 application/problem+json` listing failed deps. Probes: **MariaDB** (`SELECT 1`), **ES** (`GET /_cluster/health` + `analysis-icu` plugin loaded + every `resources/es/*.json` index exists; B26, I5), **Redis broker & rate-limiter** (`PING` against active topology per `REDIS_MODE`; B27), **MinIO** (`ListBuckets`). Each probe ≤ 2 s. |
| GET    | `/metrics`              | none        | — | `200 text/plain; version=0.0.4` — Prometheus exposition (counters/histograms in §3.7) |

Future-phase auth: JWT verify (auth) + `PermissionClient` post-retrieval gate (permission, OpenFGA-backed) — see §3.5. ES queries remain permission-blind in every phase.

**Embedding lifecycle admin routes (B50)** — zero-downtime model swap; full detail in [`docs/00_API.md §Embedding Model Lifecycle`](../00_API.md#embedding-model-lifecycle-admin):

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/embedding/v1/promote` | `X-User-Id` | Open migration; PUT ES mapping + enable dual-write → `200 {state:"CANDIDATE"}` |
| POST | `/embedding/v1/cutover` | `X-User-Id` | Switch reads to candidate (subject to preflight) → `200 {state:"CUTOVER"}` |
| POST | `/embedding/v1/rollback` | `X-User-Id` | Revert reads to stable → `200 {state:"CANDIDATE"}` |
| POST | `/embedding/v1/commit` | `X-User-Id` | Promote candidate to stable; retire old field → `200 {state:"IDLE"}` |
| POST | `/embedding/v1/abort` | `X-User-Id` | Drop candidate → `200 {state:"IDLE"}` |
| POST | `/embedding/v1/backfill` | `X-User-Id` | Enqueue backfill task → `200 {state, queued}` |
| GET  | `/embedding/v1/state` | `X-User-Id` | Registry snapshot → `200 {stable, candidate, read, retired}` |
| GET  | `/embedding/v1/cutover/preflight` | `X-User-Id` | Run gates without action → `200 {pass, gates}` |

**Other domain endpoints** — defined in their own spec files, not repeated here:

- Skills CRUD (`/skills/v1`): [`docs/spec/skills.md`](skills.md)
- ChatAgent v1/v2/v3 (`/chatagent/v1`, `/v2`, `/v3`): [`docs/spec/chatagent_v3.md`](chatagent_v3.md)
- brainagent v1 (`/brainagent/v1` run + reconnect + cancel + `/brainagent/v1/*` brain management proxy): [`docs/spec/brainagent_v1.md`](brainagent_v1.md)
- Chat Attachments (`/chatagent/v3/attachments`): [`docs/spec/chat_attachments.md`](chat_attachments.md)
- twp-ai Adapter (`/twp/v1/run`): [`docs/spec/twp_ai.md`](twp_ai.md)
