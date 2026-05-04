# Discussion: Phase 1 Review — Ingest Lifecycle + Pluggable Pipeline + 3rd-Party API Integration

> Source: user directive 2026-05-04 + new `docs/00_rule.md` + `docs/00_agent_team.md`
> Date: 2026-05-04
> Mode: RAGENT Agent Team (6 voting members + Master)
> Predecessor: `2026_05_03_phase1_kickoff.md` (Round 1 frozen P1 scope; this round refines)

---

## Master's Opening

**Triggered rules / new context:**
- `00_rule.md` §Third-Party API: Embedding (`/text_embedding`, `bge-m3`, returnCode 96200), LLM (`/gpt_oss_120b/v1/chat/completions`, stream), Rerank (`/`, `bge-reranker-base`), AI Auth (J1→J2 token exchange + 5-min refresh), **OpenFGA** (`list_resource` + `check`), HR API.
- `00_rule.md` §Standard: layered architecture (Router/Service/Repository), UUIDv7+Base32 (26 chars), end-to-end UTC ISO 8601, no physical FK, methods ≤ 30 lines, max 2-level nesting.
- `00_agent_team.md` §Workflow §6: every round must refine `00_journal.md` with deduped lessons.
- User directive: *Phase 1 must include ingest CRUD lifecycle*; pluggable pipeline must be reviewed.

**Topic:** Reconcile prior Phase 1 plan with the new rules. Three sub-topics decided in one round (per agent_team.md max-3-rounds protocol):
- **T1** Ingest CRUD lifecycle — what is missing in plan, what to add.
- **T2** Pluggable pipeline — registry, factory, DI patterns; what we pay/save in P1.
- **T3** Third-party API integration & auth — OpenFGA replaces ACL JSON column; J1→J2 token manager; bind to spec/plan.

---

## Round 1 — Role Perspectives (Pro & Con with citations)

### T1 · Ingest CRUD Lifecycle

- 🏗 **Architect**: [Pro] `00_spec.md` §3.1 names the lifecycle (`UPLOADED → PENDING → READY/FAILED`) but plan.md has **no TDD task** for repository, MinIO adapter, or `POST /ingest` endpoint — this is a documentation/implementation gap. Must insert a **W2.5 sub-week**. [Con] If we write Router/Service/Repository (`00_rule.md` §Layered) for ingest in P1, blast radius includes 4–5 new modules; argue Router+Service-only would be enough for P1 happy path. **Position**: full 3 layers, but each ≤ 30 LOC, one method one responsibility.
- ✅ **QA**: [Pro] Without lifecycle TDD, S1/S2/S3 in spec §5 cannot be verified. Add Negative Path tests per `00_journal.md` 2026-04-25 rule (state-machine transitions). [Con] Reconciler's PENDING semantics are ambiguous — does "PENDING" mean "queued and awaiting worker" or "errored, awaiting retry"? Need explicit state diagram before writing Reds. **Position**: state machine = `UPLOADED → (worker picks) → PENDING → READY | (error) PENDING (retry) → READY | FAILED`. Worker transitions UPLOADED→PENDING on pickup so crashed workers are caught by Reconciler.
- 🛡 **SRE**: [Pro] Pessimistic locking on status updates (`00_journal.md` 2026-05-04 Architecture rule) — Reconciler and Worker both write `status` and `attempt`; without `SELECT … FOR UPDATE` we get double-dispatch. [Con] FK-cascade deletion is forbidden (`00_rule.md` §Database). `chunks` ↔ `documents` cascade in current spec violates this rule. **Position**: ORM-level cascade in Service.delete_document(); no DB FK.
- 🔍 **Reviewer**: [Pro] Repository layer enables idempotent `upsert_document` (UUIDv7 PK is sortable so duplicate detection is cheap). [Con] We must NOT preemptively add `soft_delete`, `version`, `tenant_id` — YAGNI. **Position**: minimum columns; future migrations cheap with no FK.
- 📋 **PM**: [Pro] Insert W2.5 between W2 and W3 (rename W3+→W4+) — no calendar slip if we trim W6 acceptance to "ingest success rate + chaos test" (defer golden-set 50Q to P2 prep, accept risk per `00_agent_team.md` §Convergence). [Con] Re-numbering existing checkboxes risks losing trace. **Position**: keep W2/W3/W4/W5/W6 numbering; insert tasks 2.6-2.10 covering CRUD lifecycle inside W2 final days.
- 💻 **Dev**: [Pro] Use SQLAlchemy 2.x Core for Repository (no ORM declarative class for chunks/documents — keep Repository thin). MinIO via `minio` Python SDK. Both can be mocked at unit level. [Con] `00_rule.md` mandates UUIDv7+Base32 — must add `uuid7` dep + Crockford base32 encoder; can be utility 5 lines. **Position**: tiny `id_gen.py` utility, mocked clock for tests.

**T1 Conflicts:**
- C1.1: PENDING semantics = "worker picked up" vs "error retry". Vote on QA's unified definition.
- C1.2: Layering — Router+Service vs Router+Service+Repository for P1 ingest.
- C1.3: Cascade strategy — DB FK (current spec) vs ORM-level cascade (new rule).
- C1.4: Plan numbering — keep W2 + sub-tasks vs renumber.

### T2 · Pluggable Pipeline

- 🏗 **Architect**: [Pro] Plugin registry (`PluginRegistry.register(plugin) / fan_out(doc_id)`) lets workers iterate over registered `ExtractorPlugin`s without import-time coupling. Aligns with `draft.md` L170 ("掛 plugin, 主架構零改動"). [Con] A registry is a global singleton if not careful — DI it via FastAPI lifespan + Worker startup hook. **Position**: registry is a plain class, instantiated once in app/worker bootstrap, passed by reference.
- ✅ **QA**: [Pro] Registry makes "Stub Graph day-1 fan-out" testable in unit (assert `fan_out` calls both plugins). [Con] If `required` plugin fails, who decides status? Need explicit "all required must succeed" rule with test. **Position**: `fan_out` returns `dict[plugin_name, Result]`; Service decides status from this map.
- 🛡 **SRE**: [Pro] Per-plugin queue (`extract.vector`, `extract.graph`) means failures isolate; Reconciler can target a specific queue. [Con] Adds operational complexity (2+ queues per plugin). **Position**: required plugins = own queue; optional = own queue with shorter retry.
- 🔍 **Reviewer**: [Pro] Pipeline factory (`pipelines/factory.py`) hides Haystack assembly; tests inject the factory output. Method ≤ 30 LOC achievable. [Con] Reject any "plugin discovery" magic (entry-points, decorator scanning) — explicit registration only. **Position**: explicit `registry.register(VectorExtractor(...))` in bootstrap.
- 📋 **PM**: [Pro] Pluggability is the **core selling point** (`draft.md` L170) — without it, P2 rerank swap and P3 graph swap become rewrites. [Con] Don't pay for P3 in P1 — Stub Graph extractor is enough proof of plugability. **Position**: registry + Stub Graph already meet the bar.
- 💻 **Dev**: [Pro] Factory + registry + DI of API clients = 3 pieces, ≤ 100 LOC total. Supports the "import换一行" promise of P2. [Con] Make registry plugin-name-unique; raise on duplicate. **Position**: registry indexed by `plugin.name`; duplicate raises at register time.

**T2 Conflicts:**
- C2.1: Plugin discovery mechanism — explicit register vs decorator-scan. (already aligned: explicit)
- C2.2: Per-plugin queue isolation — yes vs share one queue. Vote.
- C2.3: required-plugin failure → status — `READY` (best-effort) vs `PENDING` (retry) vs `FAILED` (immediate). Vote.

### T3 · Third-Party API Integration & Auth

- 🏗 **Architect**: [Pro] **OpenFGA replaces `acl_user_ids` JSON column** — `00_rule.md` documents `list_resource` (returns `[doc-1, doc-2, ...]` for a user) and `check` (returns `{"allowed": bool}` per doc). This means MariaDB `documents` table no longer needs `acl_user_ids`; ES doesn't need `acl_user_ids` keyword field either. Pre-filter = list_resource → ES `terms` filter on `doc_id`; post-filter = `check(doc_id)` per result. [Con] OpenFGA introduces an external dep on hot path (every chat query). Need timeout (10s) + cached read (`list_resource` 60s TTL, `check` per-request). **Position**: must include in P1; cache only `list_resource` (whitelist) per request, not per session.
- ✅ **QA**: [Pro] Auth-related Givens are now testable via mocked OpenFGA (one Fake for both `list_resource` and `check`). Add S7 (permission isolation) acceptance. [Con] Need negative test: J2 token expired mid-stream — does TokenManager refresh transparently? Add Red. **Position**: TokenManager unit test must cover expiry + refresh boundary.
- 🛡 **SRE**: [Pro] **TokenManager 5-min pre-expiry refresh** is critical — `00_rule.md` says cache. Without it, every 3rd-party call exchanges fresh J1 → J2 (10s/call burn). [Con] Token cache leak / staleness if clock drift. **Position**: cache key = (j1_token, scope), refresh at `expiresAt - 5min`; assertion in test using fake clock.
- 🔍 **Reviewer**: [Pro] HTTP clients must implement retry + circuit-breaker (`draft.md` L127, "Rate Limit + Circuit Breaker + Retry"). [Con] Don't hand-roll — use `httpx` + `tenacity`; 30 LOC limit per method. **Position**: `clients/embedding.py`, `clients/llm.py`, `clients/rerank.py`, `clients/openfga.py`, `clients/auth.py`, `clients/hr.py` — each thin wrapper, retry/timeout from `00_rule.md` table.
- 📋 **PM**: [Pro] Plan must list all six clients as P1 tasks since chat E2E (S6) requires LLM stream + Embedding + ES retrieval + OpenFGA. Without these, no E2E test passes. [Con] HR API only needed if user_id resolution is required at ingest/chat — defer to W5 only if test demands. **Position**: HR API client = P1 if `/ingest` needs `owner_user_id` from JWT subject claim; otherwise P2. Vote.
- 💻 **Dev**: [Pro] All clients share a `BaseAIClient` mixin only if they share auth (Embedding/LLM/Rerank do via TokenManager). OpenFGA/HR have static `header` tokens — separate. [Con] Hidden coupling via mixin = anti-pattern; prefer composition (TokenManager as constructor arg). **Position**: composition; `class EmbeddingClient: def __init__(self, token_mgr: TokenManager, http: httpx.AsyncClient)`.

**T3 Conflicts:**
- C3.1: ACL backend = OpenFGA (new) vs JSON column (old spec). Vote.
- C3.2: HR API in P1 vs P2. Vote.
- C3.3: Cache scope of `list_resource` — per-request only vs short-TTL session. Vote.

---

## Conflict Resolution & Voting

| # | Issue | 🏗 | ✅ | 🛡 | 🔍 | 📋 | 💻 | Result |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| C1.1 | PENDING = "worker picked up OR retrying" (unified) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C1.2 | P1 ingest uses full Router+Service+Repository | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** (mandated by `00_rule.md`) |
| C1.3 | ORM-level cascade only (no DB FK) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** (mandated by `00_rule.md`) |
| C1.4 | Insert tasks 2.6–2.10 + 3.0 (renamed) for ingest CRUD | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C2.2 | Per-plugin queue isolation in P1 | ✅ | ✅ | ✅ | ❌ (YAGNI: one queue) | ✅ | ✅ | **Pass 5/6** |
| C2.3 | Required-plugin failure → status=PENDING (retry-eligible) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C3.1 | OpenFGA replaces ACL JSON column | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C3.2 | HR API in P1 (resolve owner from JWT subject) | ❌ (P2) | ✅ | ❌ (P2) | ❌ (YAGNI) | ✅ | ❌ (no consumer in P1) | **Reject 4/6 → defer to P2** |
| C3.3 | `list_resource` cached per-request only | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

---

## Decision Summary

### ✅ Approved (binding for P1)

#### Ingest Lifecycle (T1)

1. **State machine** (replaces draft.md ambiguity):
   ```
   ┌─ POST /ingest ──> documents.status = UPLOADED, kiq ingest_pipeline
   ├─ Worker pickup ─> SELECT … FOR UPDATE; status = PENDING, attempt += 1
   ├─ All required plugins OK ─> status = READY
   ├─ Any required plugin error ─> status = PENDING (Reconciler 5-min sweep)
   └─ attempt > 5 ─> status = FAILED + structured-log alert
   ```
2. **Layered modules** (mandatory, ≤30 LOC/method):
   - `routers/ingest.py` — `POST /ingest`, `GET /ingest/{document_id}` (parse + validate + delegate).
   - `services/ingest_service.py` — orchestrates: store MinIO → repo.create → kiq dispatch.
   - `repositories/document_repository.py` — `create / get / update_status / list_pending`.
   - `repositories/chunk_repository.py` — `bulk_insert / delete_by_doc_id`.
   - `storage/minio_client.py` — `put_object / delete_object`.
   - `utility/id_gen.py` — `new_id() -> str` (UUIDv7 → Crockford base32 → 26 chars).
   - `utility/datetime.py` — `utcnow_iso() -> str` (`Z` suffix).
3. **No DB FK**: `chunks.doc_id` is a plain `VARCHAR(26)` with index. ORM-cascade in `IngestService.delete()`.
4. **Pessimistic lock**: every `documents.status` mutation uses `SELECT … FOR UPDATE`.
5. **IDs**: `document_id` and `chunk_id` are 26-char Crockford base32 strings derived from UUIDv7.
6. **Timestamps**: `created_at`/`updated_at` stored as UTC; serialized as ISO 8601 `Z`.

#### Pluggable Pipeline (T2)

7. **PluginRegistry**: `src/ragent/plugins/registry.py` — `register(plugin)` (raises on duplicate name), `fan_out(doc_id) -> dict[name, Result]`. Tests assert all registered plugins called.
8. **Per-plugin queue**: `extract.vector`, `extract.graph`. TaskIQ routes by queue.
9. **Required vs optional contract**: `IngestService` reads `fan_out` result map; if any `required=True` plugin's Result is `error`, status → PENDING (retry); else READY.
10. **Pipeline factory**: `src/ragent/pipelines/factory.py` — central Haystack assembly; ingest + chat pipelines built here, swappable in P2 by import change.
11. **Bootstrap registration**: `src/ragent/bootstrap.py` — explicit `registry.register(VectorExtractor(...))`, `registry.register(StubGraphExtractor())`. No decorator-scan or entry-point magic.

#### Third-Party API & Auth (T3)

12. **OpenFGA replaces ACL JSON column** — schema change:
    - Drop `documents.acl_user_ids JSON`. Drop `chunks_v1.acl_user_ids` ES field.
    - Add `documents.owner_user_id` (FK-less reference; written in P2 when HR resolution lands).
    - Pre-filter: `openfga.list_resource(user_id, "can_view", "kms_page")` → list of `doc_id` → ES `terms` filter on `doc_id`.
    - Post-filter: `openfga.check(user_id, "can_view", "kms_page", doc_id)` per returned chunk; mismatch → drop + audit log.
    - Cache scope: `list_resource` cached **per-request only** (one call per `/chat`), not session.
13. **TokenManager** (J1 → J2) — `src/ragent/clients/auth.py`:
    - Cache key = `j1_token` value.
    - Refresh at `expiresAt - 5 min` (uses fake clock in tests).
    - K8s mode: read SA token if `AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true`; else `AI_*_J1_TOKEN` env.
14. **API clients** (`httpx.AsyncClient` + `tenacity` retry, all ≤30 LOC/method):
    - `EmbeddingClient` → `POST /text_embedding`, model `bge-m3`, validate `returnCode == 96200`.
    - `LLMClient` → `POST /gpt_oss_120b/v1/chat/completions`, `stream=True`, async iterator yielding deltas.
    - `RerankClient` → `POST /` (rerank URL), model `bge-reranker-base`. **Used in P2** — interface defined now, not wired to chat pipeline until P2 to satisfy YAGNI; tested as unit only.
    - `OpenFGAClient` → `POST /list_resource`, `POST /check`, `gam-key` header.
    - `HRClient` → P2 (deferred).

#### Plan & Spec Updates

15. **Plan structure** — insert tasks **2.6–2.10** (CRUD utilities & schema), **3.0** (state machine + repo), **3.6–3.8** (API clients + token mgr) into existing W2/W3 numbering. Re-tier W4/W5 to wire registry + OpenFGA in chat path.
16. **Spec §7.1 schema** rewritten without FK and `acl_user_ids`; PK becomes 26-char.
17. **Spec §5 BDD** add S9 (token expiry refresh), S10 (state-machine negative paths per `00_journal.md` rule), S11 (registry fan-out invariants).
18. **`00_journal.md` new lessons** — added below in §Reflection.

### ❌ Rejected / Deferred

- HR API in P1 → **P2** (no consumer until JWT-subject-to-employee resolution is needed).
- Decorator-scan plugin discovery → **never** (explicit registration only).
- Single shared TaskIQ queue for all extractors → **rejected**; each plugin owns its queue.

### 🤝 Trade-offs Accepted

- OpenFGA on hot path adds ~10–50 ms per `/chat` call → mitigated by per-request caching of `list_resource`.
- 26-char IDs > 16-char auto-increment → URL-safe + sortable + decentralized > storage cost.
- Per-plugin queue increases TaskIQ config complexity → isolation > simplicity.
- 6 thin client modules (Embedding/LLM/Rerank/OpenFGA/Auth/HR-stub) increases file count → matches `00_rule.md` "high cohesion, low coupling".

---

## Pending Items

None. All conflicts converged Round 1. Hand off to spec/plan/journal updates.

---

## Reflection — feeding `00_journal.md`

| Domain | Issue | Root Cause | Actionable Guideline |
|---|---|---|---|
| **PM/Spec** | Plan W3 jumped to "Ingest Pipeline" without CRUD lifecycle TDD; spec lifecycle existed but was not decomposed into tasks. | `draft.md` documented WHAT but plan author did not enumerate every BDD scenario as a TDD `[ ]` row. | **[Rule]** Every BDD scenario in `00_spec.md` §5 must be backed by ≥1 row in `00_plan.md` whose test path matches the scenario name. |
| **Architecture** | Spec §7.1 contained DB FK + JSON ACL column, in conflict with `00_rule.md` rules. | Spec was authored before reading the latest `00_rule.md`. | **[Rule]** Whenever `00_rule.md` is updated, spec/plan must be re-validated in the next round; team review must cite rule version. |
| **Security** | TokenManager refresh boundary (5 min pre-expiry) is silent failure if missed → all 3rd-party calls die simultaneously. | 5-min pre-expiry not initially in BDD scenarios. | **[Rule]** Every external dependency with token TTL must have a "boundary refresh" Red test using a fake clock. |

These three rows will be appended to `00_journal.md` (deduped against existing entries).
