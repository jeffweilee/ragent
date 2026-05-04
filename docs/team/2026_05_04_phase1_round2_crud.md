# Discussion: Phase 1 Round 2 — Ingest CRUD Interface, Supported Formats, Pipeline/Plugin Catalog

> Source: user directive 2026-05-04 (Round 2)
> Date: 2026-05-04
> Mode: RAGENT Agent Team (6 voting members + Master)
> Predecessor: `2026_05_04_phase1_review.md` (lifecycle/pluggable/3rd-party API decisions)

---

## Master's Opening

**Triggered context:**
- User asks to (1) define the full ingest CRUD HTTP interface and how Create/Delete cascade through Vector + Graph plugins, (2) declare a table of supported ingest data formats, (3) declare a table of pipelines and plugins.
- `00_rule.md` Layered architecture forbids Router business logic — Router only delegates; cascade lives in `IngestService`.
- `00_journal.md` 2026-05-04 Architecture rule: state-machine guarded; pessimistic lock on every mutation.

**Topics for this round:**
- **T4** CRUD HTTP interface for `/ingest` + cascade integration with `PluginRegistry.fan_out_delete()`.
- **T5** Supported ingest data formats (table) bound to Haystack converters.
- **T6** Pipelines and Plugins catalog tables (single source of truth).

---

## Round 2 — Role Perspectives

### T4 · CRUD interface + Vector/Graph integration

- 🏗 **Architect**: [Pro] Symmetry: just as ingest **fans out** `extract()` to required + optional plugins, delete must **fan out** `delete()` to **all** registered plugins (including optional) so the ES index and any future Graph DB drop in lockstep. Order matters: plugins delete first (downstream stores), then ChunkRepository, then DocumentRepository, then MinIO. [Con] If a plugin's `delete` fails, do we still proceed? Risk of orphans either way. **Position**: best-effort fan-out-delete, log per-plugin errors, mark document `status=DELETING` then physically remove only after all plugins reported (eventually-consistent); for P1 simplest = fail-fast on first plugin error and surface 500.
- ✅ **QA**: [Pro] Need GWT for DELETE happy path + partial-failure path. Also LIST endpoint needs pagination Given-When-Then. [Con] PATCH/PUT for ACL is moot now that OpenFGA owns ACL — don't add. **Position**: P1 endpoints = `POST/GET/LIST/DELETE`. No PUT/PATCH.
- 🛡 **SRE**: [Pro] DELETE must be idempotent (re-DELETE on missing returns 204, not 404 — survives retries). Cascade order matters for blast radius; isolate Graph DB failure from Vector index. [Con] Fail-fast leaves a half-deleted document — operationally bad. **Position**: introduce `DELETING` transient state; Reconciler also sweeps `DELETING > 5min` and retries fan-out-delete.
- 🔍 **Reviewer**: [Pro] LIST must enforce OpenFGA pre-filter — only return documents whose `document_id ∈ list_resource(user_id)`. [Con] Exposing `/ingest` LIST without pagination is a footgun. **Position**: cursor pagination (`?after=<document_id>&limit=≤100`); response includes `next_cursor`.
- 📋 **PM**: [Pro] CRUD complete is needed for any UI/CLI integration; without DELETE, demo cannot tear down test data. [Con] LIST adds W3 scope. **Position**: fold LIST into existing W3 service tests; no calendar slip.
- 💻 **Dev**: [Pro] Single `IngestService.delete(document_id, user_id)` that calls `PluginRegistry.fan_out_delete(document_id)` mirrors fan_out symmetry; all 30 LOC, no nesting. [Con] OpenFGA `check(user_id, "can_edit"|"can_delete", document_id)` needed before delete — adds a second relation to OpenFGA modeling. **Position**: P1 reuses `can_view` for delete (acceptable risk noted in journal); P2 introduces `can_delete` relation when HR+roles land.

### T5 · Supported Ingest Data Formats

- 🏗 **Architect**: [Pro] Bind to Haystack 2.x converters by default — they cover .txt/.md/.pdf/.docx/.pptx/.html/.csv/.xlsx without custom code. [Con] OCR for image-PDF in P1 is a rabbit hole (Tesseract dep, language packs). **Position**: P1 = text-extractable formats only; image-only PDFs / pure images = P2 with `OCRRouter` SuperComponent.
- ✅ **QA**: [Pro] Each supported format needs at least one fixture file in `tests/fixtures/` and a smoke E2E. [Con] PPTX has table/notes — must declare WHICH parts get extracted (slide text + speaker notes; embedded images skipped P1). **Position**: declare extraction surface per format; smoke fixtures per format gating P1 acceptance.
- 🛡 **SRE**: [Pro] MIME sniffing + size cap (e.g., 50 MB P1) at Router; reject early. [Con] Anti-virus scanning out of scope. **Position**: 50 MB cap, MIME allow-list at Router, content-type re-validated at Service.
- 🔍 **Reviewer**: [Pro] Table makes the "what's supported" question reviewable in one place. [Con] Don't declare formats we won't ship in P1 — keep the table honest with a Phase column. **Position**: format table includes Phase column; P2/P3 rows present but clearly marked.
- 📋 **PM**: [Pro] Table is also the answer to enterprise customers' first question. [Con] Avoid premature commitments to .xlsx (table reasoning is a known weak spot). **Position**: .xlsx in P2 with caveat; P1 includes .csv only.
- 💻 **Dev**: [Pro] Haystack `FileTypeRouter` already routes by MIME — one line of code per format. [Con] Avoid hand-rolling format detection. **Position**: `FileTypeRouter` + per-format `*ToDocument` converter; no custom detection.

### T6 · Pipelines & Plugins Catalog Tables

- 🏗 **Architect**: [Pro] Two tables — Pipelines (Ingest, Chat) listing components and pluggable points; Plugins (extractors) listing name/required/queue/Phase. Single source of truth; plan rows reference these tables. [Con] Risk of doc/code drift. **Position**: tables in spec §6.6; lint rule (Phase 2) cross-checks `PluginRegistry.registered_names()` against table.
- ✅ **QA**: [Pro] Each row in the Plugin table maps to a `tests/unit/test_<plugin>_*.py`. [Con] Pipeline table needs an "integration test path" column. **Position**: add Test Path column to both tables.
- 🛡 **SRE**: [Pro] Per-plugin queue surfaced in the table → ops can size workers per queue. [Con] Pipeline table should also list timeouts. **Position**: include "Timeout/Retry" column for pipelines and plugins.
- 🔍 **Reviewer**: [Pro] Tables enforce naming consistency (`name`, `queue` exactly as code). [Con] Tables become stale if not enforced. **Position**: Phase 2 CI lint compares table to registry; until then, manual review at end of each round.
- 📋 **PM**: [Pro] Catalog enables roadmap conversations ("when does Rerank ship?" → table column). [Con] Don't re-document phasing already in §1/§2. **Position**: tables show Phase only; rationale stays in §1/§2.
- 💻 **Dev**: [Pro] When implementing a new plugin, table is checklist (name, required, queue, queue worker config, test path). [Con] Don't add table fields nobody enforces. **Position**: minimum viable columns.

---

## Voting Results

| # | Issue | 🏗 | ✅ | 🛡 | 🔍 | 📋 | 💻 | Result |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| C4.1 | DELETE: introduce transient `DELETING` state + Reconciler sweep | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C4.2 | DELETE order: plugins → chunks → document → MinIO | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C4.3 | DELETE auth: P1 reuses `can_view`; P2 adds `can_delete` (Accepted Risk) | ✅ | ✅ | ❌ (security: weak in P1) | ✅ | ✅ | ✅ | **Pass 5/6** with noted risk |
| C4.4 | LIST endpoint: cursor pagination + OpenFGA pre-filter | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C4.5 | DELETE idempotent (re-delete returns 204) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C5.1 | P1 formats = .txt .md .pdf .docx .pptx .html .csv | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C5.2 | 50 MB cap + MIME allow-list at Router | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C5.3 | Image OCR / .xlsx → P2 (declared but disabled) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C6.1 | Pipeline + Plugin catalog tables in spec §6.6 with Test Path + Timeout/Retry columns | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

---

## Decision Summary

### CRUD Interface (T4) — Approved

| Method | Path | Purpose | Notes |
|---|---|---|---|
| `POST` | `/ingest` | Create | multipart, returns 202 + `task_id` |
| `GET` | `/ingest/{document_id}` | Read | status + attempt + updated_at |
| `GET` | `/ingest` | List | cursor pagination; OpenFGA pre-filter |
| `DELETE` | `/ingest/{document_id}` | Delete | idempotent (204 on missing); `DELETING` transient state |

**Cascade delete order** (in `IngestService.delete`):
1. OpenFGA `check(user_id, "can_view", document_id)` → 403 if denied (P1 reuse `can_view`; P2 → `can_delete`).
2. `DocumentRepository.acquire(document_id)` → `status = DELETING`.
3. `PluginRegistry.fan_out_delete(document_id)` → calls `delete()` on **every** registered plugin (required + optional). Vector deletes its ES bulk; Graph (P3) deletes entities/edges; Stub no-ops.
4. `ChunkRepository.delete_by_document_id(document_id)`.
5. `MinIOClient.delete_object(storage_uri)`.
6. `DocumentRepository.delete(document_id)`.

If any step fails → row stays `DELETING`; **Reconciler** also sweeps `status=DELETING AND age>5min` and retries the fan-out-delete (idempotent — plugins must tolerate "already deleted").

### Supported Ingest Data (T5) — Approved (see spec §6.5)

P1 formats: `.txt .md .pdf (text-extractable) .docx .pptx .html .csv` (per Haystack 2.x converters).
P2: `.xlsx`, image-PDF (OCR), pure images.
Limits: 50 MB cap, MIME allow-list at Router.

### Pipelines & Plugins Catalog (T6) — Approved (see spec §6.6)

Two tables added to spec §6.6 Pipeline Catalog and §6.7 Plugin Catalog.

### Plan additions (T4–T6)

- W3 row 3.7 expanded: `IngestService` now covers create + delete + list (still 1 service, ≤30 LOC/method).
- W3 add row 3.7d: `tests/unit/test_ingest_service_delete.py` — cascade order; partial failure → DELETING; idempotent re-delete.
- W3 add row 3.7l: `tests/unit/test_ingest_service_list.py` — pagination + OpenFGA pre-filter.
- W3 add row 3.0d: `PluginRegistry.fan_out_delete()` test (S5 extended).
- W3 add row 3.4s: `DocumentRepository.update_status` accepts `DELETING` (state machine extended: PENDING→DELETING, READY→DELETING, FAILED→DELETING; DELETING→FAILED on attempt>5).
- W6 add row 6.1d: Reconciler also sweeps `DELETING > 5min`.

### Accepted Risks

- **C4.3**: P1 delete authorization uses `can_view` (any reader can delete). Tracked as a P2 must-fix; note in `00_journal.md`. Mitigation: audit log on every delete.

---

## Pending Items

None — Round 2 converged 6/6 on all topics except C4.3 (5/6 with documented Accepted Risk per `00_agent_team.md` §Convergence).

---

## Reflection — feeding `00_journal.md`

| Domain | Issue | Root Cause | Actionable Guideline |
|---|---|---|---|
| **API/Spec** | Original spec only had POST + GET; DELETE/LIST omitted. | "Read"/"Update" interpreted as the lifecycle status read; CRUD interface coverage not audited. | **[Rule]** Every resource exposed via `/<resource>` must declare ≥4 endpoints (Create / Read / List / Delete) or explicitly justify omission in spec §6.1. |
| **Security** | DELETE in P1 reuses `can_view` relation — broad permission for a destructive op. | OpenFGA model lacks `can_delete` relation; HR/role data deferred to P2. | **[Rule (Accepted Risk)]** Until P2 ships `can_delete`, every delete writes an audit log including (user_id, document_id, ts). P2 plan row 7.5 must add the relation before HR rollout. |
| **Spec discipline** | Pipeline & Plugin information was scattered across §2/§3/§4/§6.2; no canonical catalog. | No "single source of truth" pattern enforced for inventories. | **[Rule]** Inventories (formats, pipelines, plugins, third-party APIs, env vars) live in dedicated spec tables; prose sections reference them, not vice versa. |
