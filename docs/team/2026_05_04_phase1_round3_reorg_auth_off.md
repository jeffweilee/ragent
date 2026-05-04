# Discussion: Phase 1 Round 3 — Spec/Plan Reorganization + Disable Auth/OpenFGA

> Source: user directive 2026-05-04 (Round 3)
> Date: 2026-05-04
> Mode: RAGENT Agent Team (6 voting members + Master)
> Predecessors: `2026_05_04_phase1_review.md` (lifecycle/pluggable), `2026_05_04_phase1_round2_crud.md` (CRUD/formats/catalogs)

---

## Master's Opening

**Triggered context:**
- User asks to (1) reorganize `00_spec.md` and `00_plan.md` into domain-grouped, bullet-readable sections, and (2) **disable Auth + OpenFGA layers in P1** (defer wiring to P2).
- `00_rule.md` §Layered Responsibilities still applies — reorg must not flatten Router/Service/Repository.
- `00_journal.md` 2026-05-04 "single source of truth" rule: inventories belong in tables, prose references them.
- Round-2 Accepted Risk on `can_view`-as-delete-auth becomes moot if the entire auth layer is off — replaced by a stronger but explicit risk: "P1 runs in OPEN mode."

**Topics this round:**
- **T7** Reorganize spec by **domain** (Ingest / Indexing / Plugins / Retrieval / Auth / Resilience / Observability) instead of by section type.
- **T8** Reorganize plan by **domain track** instead of by week (W1–W6) — week metadata still encoded but as an attribute, not the structure.
- **T9** Disable Auth + OpenFGA in P1: define exactly what "open mode" means, how user_id is sourced for tests, and what is preserved vs deleted.

---

## Round 3 — Role Perspectives

### T7 · Spec reorganization by domain

- 🏗 **Architect**: [Pro] Domain grouping matches DDD bounded contexts (`00_rule.md` Architect responsibility); reduces cross-section jumping when reviewing one feature. [Con] Risk of duplicating BDD scenarios across domains. **Position**: BDD lives **inside its owning domain**; cross-domain scenarios are explicitly listed once in the receiving domain.
- ✅ **QA**: [Pro] Easier to verify "every BDD scenario has a plan row" if both docs share the same domain headings. [Con] Scenario IDs (S1, S2…) must remain stable so journal/plan back-references don't rot. **Position**: keep S-numbers as immutable IDs; section moves don't renumber.
- 🛡 **SRE**: [Pro] Resilience domain (Reconciler, retries, locks) becomes a single discoverable section. [Con] Observability is mostly cross-cutting; resist over-isolating it. **Position**: Observability is its own domain but explicitly notes its hooks live across all other domains.
- 🔍 **Reviewer**: [Pro] Easier to spot YAGNI by seeing each domain's full surface. [Con] Inventories (formats, pipelines, plugins, APIs) must remain a **single** flat block (Round-2 rule), not sprinkled. **Position**: inventories collected in `§4 Inventories`, prose references them via section number.
- 📋 **PM**: [Pro] Stakeholders read by feature ("what's our auth story?"); domain layout matches that mental model. [Con] Long table-of-contents — must keep page navigable. **Position**: every domain has a 3-line summary at top; deep details below.
- 💻 **Dev**: [Pro] When implementing a module I land in one domain section and have everything (process + BDD + interface + data) without scrolling. [Con] Don't bury the data schema inside Ingest domain — others (Chat) read from same tables. **Position**: schemas remain in dedicated `§5 Data Structures`; domains link to them.

### T8 · Plan reorganization by domain track

- 🏗 **Architect**: [Pro] Tracks (Foundations / Plugins / Ingest CRUD / Pipelines / Third-Party Clients / Resilience / MCP / Acceptance / **Auth Disabled**) make critical-path obvious; week becomes an `Eta` attribute on each task. [Con] Without weeks, scheduling is fuzzy. **Position**: keep `Week` column on each row so PM gantt is recoverable.
- ✅ **QA**: [Pro] Each track has its own DoD subset (e.g., Plugins track DoD = registry contract tests + extractor unit tests). [Con] Cross-track dependencies (Ingest CRUD needs Foundations) must be explicit. **Position**: add a "Depends On" column citing prior task IDs.
- 🛡 **SRE**: [Pro] Resilience track is now one place to audit all locking + idempotency tests. [Con] Acceptance track must reference all other tracks. **Position**: Acceptance is the last track; lists what must be green from each prior track.
- 🔍 **Reviewer**: [Pro] Domain tracks expose imbalance (e.g., too many Plugins tasks vs Resilience). [Con] Don't lose the existing `[x]`/`[ ]` markers and their commit traces. **Position**: every existing checked task migrates with its `[x]` and reference to the commit/PR that closed it.
- 📋 **PM**: [Pro] Tracks parallelize cleanly to multiple devs. [Con] Disabling Auth removes a whole track; must be an explicit deferred section, not silent deletion. **Position**: Auth track stays in plan but tagged **DISABLED IN P1 → P2**, with all rows preserved.
- 💻 **Dev**: [Pro] One file per dev to grab. [Con] Use clear notation for "this row's deliverable currently shipped" vs "still TODO". **Position**: `[x]` means delivered; `[ ]` means TODO; `[~]` means scaffolded-but-disabled (auth track).

### T9 · Disable Auth + OpenFGA in P1

- 🏗 **Architect**: [Pro] Defers a non-trivial integration (OpenFGA model + relations + caches) so we ship a working RAG faster. [Con] All four security controls go away simultaneously: JWT, ACL pre-filter, ACL post-filter, audit log. **Position**: P1 = OPEN mode with explicit, prominent banner in spec §1. P2 = re-enable end-to-end before any production traffic.
- ✅ **QA**: [Pro] Tests get simpler (no OpenFGA fakes). [Con] BDD S7 (dual-filter) becomes a "P2 must restore" assertion. **Position**: keep S7/S15 in spec but prefix with `[P2 — Auth disabled in P1]`; QA still writes red/green for the rest.
- 🛡 **SRE**: [Strong Con] Open endpoints in any environment beyond a sealed dev cluster is a security incident waiting to happen. [Pro] If we **explicitly** restrict to dev clusters + bind 127.0.0.1 only, risk is contained. **Position**: P1 binaries refuse to start unless `RAGENT_AUTH_DISABLED=true` AND `RAGENT_ENV=dev`; otherwise fail-fast at startup.
- 🔍 **Reviewer**: [Pro] Reduces P1 surface to review. [Con] Code that's "scaffolded but disabled" rots; either delete it or compile-test it. **Position**: do not write disabled scaffolding code in P1 at all; define interfaces in spec only. P2 implements from scratch against the still-published interfaces.
- 📋 **PM**: [Pro] Faster P1 demo to stakeholders without security-team review delays. [Con] Customer/legal asks "is this safe?" — must have a written disable banner. **Position**: README + spec §1 carry a red "P1 OPEN MODE" notice; disable cannot ship beyond internal demo without P2 sign-off.
- 💻 **Dev**: [Pro] No JWT / OpenFGA / TokenManager-for-auth complexity in P1. [Con] Need a stable `user_id` source so Service layer keeps its signature (avoids breaking change at P2). **Position**: in P1, `user_id` comes from `X-User-Id` request header (dev convenience); Service signature unchanged; P2 swaps the dependency to `verify_jwt`.

---

## Conflict Resolution & Voting

| # | Issue | 🏗 | ✅ | 🛡 | 🔍 | 📋 | 💻 | Result |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| C7.1 | Spec by domain; BDD lives inside owning domain; S-numbers immutable | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C7.2 | Inventories stay in dedicated §4 (formats, pipelines, plugins, APIs) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C7.3 | Data schemas stay in §5 (referenced from domains) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C8.1 | Plan by domain track; week is an attribute column | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C8.2 | Add "Depends On" column for cross-track dependencies | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C8.3 | Auth track preserved with rows tagged `[~] DISABLED → P2`; existing `[x]` retained | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C9.1 | P1 = OPEN mode; banner in spec §1 + README | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C9.2 | Startup guard: refuse unless `RAGENT_AUTH_DISABLED=true` AND `RAGENT_ENV=dev` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C9.3 | `user_id` from `X-User-Id` header in P1; Service signature unchanged | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C9.4 | No scaffolded auth code in P1 — interfaces published in spec only; P2 implements | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C9.5 | BDD S6/S7/S15 retained but prefixed `[P2 — Auth disabled in P1]`; chat path replaces ACL pre-filter with no-op | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C9.6 | Round-2 Accepted Risk (`can_view`-as-delete) **superseded** by C9.1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

All votes 6/6 — no pending items.

---

## Decision Summary

### Spec reorganization (T7)

```
1. Mission & Scope (incl. P1 OPEN MODE banner)
2. Phase 1 In/Out
3. Domains
   3.1 Ingest Lifecycle             — process, BDD (S1, S2, S3, S10, S12, S13, S14, S15), endpoints
   3.2 Indexing Pipeline            — process, BDD (—), pluggable points
   3.3 Pluggable Extractors         — Protocol v1, registry, BDD (S4, S5, S11), catalog ref
   3.4 Retrieval & Chat             — process, BDD (S6 [P2-gated]), endpoints
   3.5 Auth & Permission            — DISABLED IN P1 → P2; interface published; BDD (S7 [P2], S9 [P2])
   3.6 Resilience                   — Reconciler, locking, BDD references
   3.7 Observability                — auto-trace, structured logs (cross-cutting)
4. Inventories
   4.1 REST/SSE Endpoints
   4.2 Supported Ingest Data
   4.3 Pipeline Catalog
   4.4 Plugin Catalog
   4.5 Third-Party API Catalog
5. Data Structures
   5.1 MariaDB
   5.2 Elasticsearch
   5.3 ID + DateTime utilities
6. Standards reference (links to 00_rule.md)
```

### Plan reorganization (T8)

Tracks (each row gets columns: `#`, `Category`, `Task`, `Depends On`, `Status`, `Owner`, `Week`):

| Track | Notes |
|---|---|
| **T0 Foundations** | id_gen, datetime utility, state machine | W2.5 |
| **T1 Plugins** | Protocol v1, StubGraph, PluginRegistry, fan_out_delete, VectorExtractor | W2–W3 |
| **T2 Ingest CRUD** | DocumentRepository, ChunkRepository, MinIOClient, IngestService (create/delete/list), Router | W3 |
| **T3 Pipelines** | Haystack ingest pipeline, chat pipeline | W3–W4 |
| **T4 Third-Party Clients** | TokenManager, EmbeddingClient, LLMClient, RerankClient (unit-only P1) | W4 |
| **T5 Resilience** | Reconciler (PENDING + DELETING sweeps), failure path | W6 |
| **T6 MCP Schema** | OpenAPI publish + 501 handler | W6 |
| **T7 Acceptance** | E2E success rate, golden Q&A, chaos | W6 |
| **T8 Auth & Permission** | **`[~] DISABLED IN P1 → P2`**: JWT, OpenFGAClient, ACL pre/post filter, isolation tests | (P2) |

### Auth disable (T9) — exact P1 contract

- All endpoints accept `X-User-Id` header (string, ≥ 1 char) as the user identity for any test that needs one. No JWT decode in P1.
- `IngestService.create / delete / list` accept `user_id` parameter (signature unchanged); in P1 it's the header value.
- `ChatService.stream(user_id, query)` — `user_id` is recorded for log tracing only; **no ACL filtering**. Pre-filter is `terms(document_id ∈ ALL)` (no-op); post-filter is identity.
- **Startup guard** (`src/ragent/bootstrap.py`): if not `(RAGENT_AUTH_DISABLED=true AND RAGENT_ENV=dev)`, raise `SystemExit("P1 build refuses to start in auth-disabled mode outside dev")`.
- README + spec §1 banner explicitly state P1 is OPEN mode.
- No auth code is written in P1; `src/ragent/auth/`, `src/ragent/clients/openfga.py`, `src/ragent/clients/jwt.py` are **not created** in P1. Interfaces remain in spec §3.5 / §4.5 as the P2 contract.
- Cascade delete in P1 skips OpenFGA `check`. The Round-2 Accepted Risk for `can_view`-as-delete-auth is **superseded** — auth is fully off; risk is now "P1 OPEN mode".

---

## Pending Items

None. All 12 votes 6/6.

---

## Reflection — feeding `00_journal.md`

| Domain | Issue | Root Cause | Actionable Guideline |
|---|---|---|---|
| **Security** | P1 ships OPEN: no JWT, no OpenFGA, no audit log on destructive ops. | Scope reduction to accelerate Phase 1 demo. | **[Rule (Accepted Risk)]** P1 binaries refuse to start unless `RAGENT_AUTH_DISABLED=true AND RAGENT_ENV=dev`. Bind to 127.0.0.1 only in dev. P2 must restore JWT + OpenFGA dual-filter + audit before any non-dev deployment. |
| **Documentation** | Prior spec/plan grew organically; readers paid linear-time cost to find one feature's full surface. | No domain-grouping convention. | **[Rule]** Spec is structured by domain (`§3.X` per bounded context), with cross-cutting inventories in `§4` and shared schemas in `§5`. Plan is structured by domain track with `Depends On` and `Week` attributes; never re-number BDD scenario IDs across reorganizations. |
