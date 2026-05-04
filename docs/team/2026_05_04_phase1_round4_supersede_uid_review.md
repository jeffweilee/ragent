# Discussion: Phase 1 Round 4 — Supersede / UID / MinIO Review

> Source: user directive 2026-05-04 (Round 4)
> Date: 2026-05-04
> Mode: RAGENT Agent Team (6 voting members + Master)
> Predecessors: `2026_05_04_phase1_round3_reorg_auth_off.md` (12/12 6-of-6)

---

## Master's Opening

**Triggered context:**
- Recent commits shipped: smart-upsert via `source_id` supersede on READY (`fb6ddf8`), MinIO transient staging + OCR/OpenFGA removal (`d65d3d9`), Round-3 reorg + auth-off (`286b446`).
- User directive: "follow UID rule mentioned in rule.md" — audit every identifier-like field across spec/plan.
- The supersede design is new; race conditions, idempotency, and reconciler interaction have not been adversarially reviewed.
- MinIO is now declared **transient staging only**; the deletion timing relative to `status=READY` commit needs scrutiny.

**Topics this round:**
- **T10** UID rule compliance — confirm internal PKs are `CHAR(26)` Crockford Base32; confirm `source_id` is client-supplied (UID rule does **not** apply); confirm `task_id == document_id`.
- **T11** Supersede correctness — race when two documents with the same `source_id` finish `READY` out-of-creation-order; idempotency under TaskIQ redelivery; index sufficiency; mid-flight DELETE; no-prior-doc no-op.
- **T12** MinIO transient-staging timing — worker crash window between `MinIO.delete_object` and `status=READY` commit; reconciler-retry 404 path.
- **T13** BDD coverage — every `S<n>` referenced in plan must be defined in spec; orphan PUT/PATCH references; supersede explicitly stated as the Update mechanism.

---

## Round 4 — Role Perspectives

### T10 · UID rule compliance

- 🏗 **Architect**: [Pro] `documents.document_id` and `chunks.chunk_id` are already `CHAR(26)`; rule honored. [Con] Spec §4.1 returns `task_id` from `POST /ingest` but never states `task_id == document_id`. Implicit contract is fragile. **Position**: state explicitly that `task_id` is the same 26-char Base32 string as `document_id`; never introduce a separate task identifier.
- ✅ **QA**: [Pro] S1 already asserts "26-char task_id". [Con] If implementers read §4.1 in isolation they may invent a TaskIQ-issued task_id (UUIDv4) that is not Base32-26. **Position**: add a one-line note in §4.1 binding `task_id` to `document_id`.
- 🛡 **SRE**: [Pro] `source_id VARCHAR(128)` is unbounded client input; rule §ID Generation Strategy explicitly governs **internal** PKs only. [Con] Spec doesn't make this exemption explicit; a future contributor may "fix" `source_id` to `CHAR(26)` and break clients. **Position**: annotate `source_id` as **client-supplied external identifier**; UID rule does not apply.
- 🔍 **Reviewer**: [Pro] `owner_user_id VARCHAR(64)` is also external (HR `employee_id`); same exemption applies. [Con] No comment in schema. **Position**: add a single line under §5.1 distinguishing internal IDs (`document_id`, `chunk_id`) from external IDs (`source_id`, `owner_user_id`).
- 📋 **PM**: [Pro] Clarification is cheap and prevents future contract debates. **Position**: ship the clarifications in this round.
- 💻 **Dev**: [Pro] `task_id == document_id` simplifies the implementation: `POST /ingest` returns `{ task_id: document_id }`; no second identifier path. **Position**: confirm; also rename in tests if necessary so the assertion is explicit.

### T11 · Supersede correctness

- 🏗 **Architect**: [**Strong Con — bug**] The current rule "delete prior READY docs with `created_at < self.created_at`" is **not** order-safe. Counter-example: D1 created at t=0, D2 created at t=1; D2 finishes READY at t=10 (D1 still PENDING) — D2's supersede finds nothing; D1 finishes READY at t=20 — D1's supersede looks for `created_at < D1.created_at` and also finds nothing. **Both D1 and D2 remain `READY`, violating "latest survives".** [Con] Fix by inverting the rule: when a doc reaches READY, look up the **maximum** `created_at` among all `READY` rows with same `source_id`; if `self.created_at < max`, delete **self**; else delete every other `READY` row. **Position**: change supersede semantics to "keep the row with MAX(created_at); delete the rest"; query under `FOR UPDATE SKIP LOCKED` on the full set. This collapses both branches.
- ✅ **QA**: [Pro] Architect's fix is testable with a deterministic clock. [Con] S17 wording must be updated to cover the out-of-order finish case; add S20. **Position**: add **S20 supersede out-of-order finish** to spec; update T3.2c test plan to cover it.
- 🛡 **SRE**: [Pro] TaskIQ at-least-once delivery means supersede may run 2–N times per document. The "delete others where created_at != self.created_at AND status='READY'" rule is naturally idempotent (second run finds no peers, since the loser was already deleted/superseded). [Con] If two supersede tasks for D1 and D2 race, both must converge to a single survivor; `FOR UPDATE SKIP LOCKED` on the candidate set guarantees serialization within source_id partition. **Position**: idempotency held under the new rule; index `(source_id, status, created_at)` already covers the new query.
- 🔍 **Reviewer**: [Pro] Edge case "client POSTs new doc, then DELETEs it before READY" — supersede only fires on READY; DELETE moves status to `DELETING` so the READY transition never happens, supersede never enqueued. ✓ Already correct. [Pro] Edge case "no prior doc" — query returns empty set, no-op. ✓. [Con] Spec doesn't state these explicitly. **Position**: add one sentence under §3.1 supersede paragraph: "If status leaves `PENDING` for any state other than `READY`, supersede is **not** enqueued."
- 📋 **PM**: [Pro] Customer-facing behavior of "latest write wins" is preserved by the corrected rule. **Position**: accept Architect's fix; update S17 and add S20.
- 💻 **Dev**: [Pro] Implementation cost of the fix is minimal: change `WHERE created_at < self.created_at` to `WHERE created_at <> self.created_at AND created_at <= (SELECT MAX(created_at) FROM documents WHERE source_id=? AND status='READY')` — or simpler: select all `READY` rows for `source_id`, keep the row with MAX(created_at), delete the rest in one transaction. **Position**: prefer the simpler "MAX wins" implementation in `IngestService::supersede`.

### T12 · MinIO transient-staging timing

- 🏗 **Architect**: [**Con — bug**] Current spec says: terminal state → delete MinIO object → `status=READY`. If the worker crashes between MinIO delete and status commit, the row stays `PENDING`; the reconciler re-kiqs `ingest.pipeline`; the pipeline re-fetches the (now-deleted) MinIO object → 404. The next attempt counter rises; eventually `attempt > 5` → `FAILED`. **A successfully-converted document is dropped because of a crash window.** [Con] Fix: commit `status=READY` **first**, then delete MinIO best-effort (swallow errors; reconciler may sweep orphan objects later). **Position**: invert the order — status commit precedes MinIO delete; orphan MinIO objects acceptable.
- ✅ **QA**: [Pro] Inverting order is testable: simulate exception between commit and delete; assert document is `READY` and MinIO object exists. **Position**: add **S21 worker-crash MinIO orphan tolerated** scenario; T3.2a/T3.2b updated.
- 🛡 **SRE**: [Pro] Orphan MinIO objects are bounded by attempt count and easy to GC. [Con] Need a sweeper or TTL. **Position**: add a deferred P2 task: "orphan MinIO sweeper (TTL 24h on staging objects)." For P1 ship, accept orphans with structured log `event=minio.orphan_object`.
- 🔍 **Reviewer**: [Pro] On `FAILED` (attempt > 5) we should still delete MinIO; same inversion applies — set `FAILED` first, then delete. **Position**: rule is "status commit always precedes MinIO delete, regardless of terminal branch."
- 📋 **PM**: [Pro] No customer-facing change; correctness improved. **Position**: accept.
- 💻 **Dev**: [Pro] One-line change in worker; remove the "delete MinIO before READY" path. **Position**: confirm.

### T13 · BDD coverage / orphans

- 🏗 **Architect**: [**Con — defect**] Plan rows reference `S2`, `S3`, `S7`, `S8` that **do not exist** in spec §3.X anymore (likely lost in the Round-3 reorg). Specifically: T5.1 cites `S2`, T5.3 cites `S3`, T6.2 cites `S8`. **Position**: re-introduce S2 (reconciler PENDING re-kiq), S3 (reconciler attempt>5 → FAILED), S8 (MCP returns 501) into spec §3.6 / §3.5 respectively. Drop S7 if no consumer.
- ✅ **QA**: [Pro] Round-3 journal rule says "BDD scenario IDs are immutable across reorganizations" — they were dropped, not just renumbered. **Position**: restore them with their original IDs. Also confirm S20/S21 added (above).
- 🛡 **SRE**: [Pro] S2 and S3 are the resilience contract; missing them in spec is a real coverage gap. **Position**: hard requirement to restore.
- 🔍 **Reviewer**: [Pro] Spot-check: no spec/plan reference to PUT/PATCH `/ingest`; supersede-via-`source_id` is the documented Update path. ✓ No orphans there. **Position**: add a one-line note in §3.1 stating "Mutation = re-POST with same `source_id`; there is no PUT/PATCH endpoint."
- 📋 **PM**: [Pro] DoD says "every BDD scenario in spec has a plan row whose test path matches" — currently the inverse is violated (plan rows cite missing S-numbers). **Position**: fix in this round; do not let it ride to W3.
- 💻 **Dev**: [Pro] Restoring S2/S3/S8 is editorial; cost is minutes. **Position**: confirm.

---

## Conflict Resolution & Voting

| # | Issue | 🏗 | ✅ | 🛡 | 🔍 | 📋 | 💻 | Result |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| C10.1 | Spec §4.1 explicitly binds `task_id == document_id` (26-char Base32) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C10.2 | Spec §5.1 annotates `source_id` and `owner_user_id` as external IDs (UID rule N/A) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C11.1 | Supersede semantics changed to "keep row with MAX(created_at) per source_id; delete others"; fixes out-of-order-finish bug | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C11.2 | Add **S20** (supersede out-of-order finish) to spec; update T3.2c | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C11.3 | Spec states explicitly: supersede only enqueued on `READY`; never on FAILED/DELETING | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C12.1 | Worker commits `status=READY`/`FAILED` **before** deleting MinIO; orphans tolerated | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C12.2 | Add **S21** (worker crash post-status-commit leaves MinIO orphan; document is `READY`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C12.3 | Add P2 task: orphan MinIO sweeper (TTL 24h) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C13.1 | Restore **S2** (Reconciler PENDING > 5 min re-kiq) into spec §3.6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C13.2 | Restore **S3** (Reconciler attempt > 5 → FAILED) into spec §3.6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C13.3 | Restore **S8** (MCP returns 501) into spec §3.4 / §4.1 reference | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| C13.4 | Spec §3.1 explicitly states: no PUT/PATCH; mutation = re-POST with same `source_id` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

All 12 votes 6/6 — no pending items.

---

## Output / Decisions

### Spec edits (`docs/00_spec.md`)

1. **§3.1 Supersede paragraph** — change semantics:
   - Old: "cascade-deletes every prior READY document sharing the same `source_id` (created_at strictly earlier)"
   - New: "selects all READY documents sharing the same `source_id`, keeps the one with `MAX(created_at)`, and cascade-deletes every other row."
   - Add: "Supersede is enqueued **only** on the `PENDING → READY` transition. Any other terminal transition (FAILED) or mid-flight DELETE prevents enqueue."
   - Add: "Mutation = re-POST with the same `source_id`. There is no PUT/PATCH endpoint."
2. **§3.1 Create flow step 2** — invert order: "commit `status=READY` (or `FAILED`); then delete MinIO object best-effort (errors logged as `event=minio.orphan_object`)."
3. **§3.1 BDD** — add S20 (supersede out-of-order finish), S21 (worker crash post-commit MinIO orphan).
4. **§3.4 Chat** — append a sentence: "**S8** `POST /mcp/tools/rag` → 501 in P1 (handler not yet wired)."
5. **§3.6 Resilience** — append: "**S2** `PENDING > 5 min, attempt ≤ 5` → reconciler re-kiqs `ingest.pipeline` idempotently. **S3** `attempt > 5` → status `FAILED` + structured log `event=ingest.failed`."
6. **§4.1 Endpoints** — add note under POST /ingest: "`task_id` is identical to the `document_id` (26-char Crockford Base32)."
7. **§5.1 MariaDB** — add a one-line annotation distinguishing internal IDs (CHAR(26), §ID rule applies) from external IDs (`source_id`, `owner_user_id` — client/HR-supplied; UID rule does **not** apply).

### Plan edits (`docs/00_plan.md`)

1. **T3.2c** test description: rewrite to match new supersede semantics — "keep MAX(created_at) per source_id"; reference S17 + S20.
2. **T3.2b** test description: assert "MinIO delete happens **after** status commit; `status=READY` even if MinIO delete raises"; reference S21.
3. **T3.2a** unchanged in intent but update wording: cleanup is best-effort post-commit.
4. **T2.1** repository test list: replace `list_superseded(source_id, before_ts)` with `list_ready_by_source(source_id)` (returns all READY rows; service picks MAX in Python or SQL).
5. **Phase 2** — add row P2.9: "Orphan MinIO sweeper (TTL 24h on staging objects)."

### Journal addition (`docs/00_journal.md`)

One new lesson — **State-machine commit ordering vs. side-effect cleanup** — distinct from existing "double-dispatch / locking" rule.

---

## Reflection — feeding `00_journal.md`

| Domain | Issue | Root Cause | Actionable Guideline |
|---|---|---|---|
| **Architecture** | Worker deleted MinIO staging object **before** committing terminal status, creating a crash window where the document is lost on retry. | Side-effect cleanup ordered before durable state commit. | **[Rule]** Within any worker, **durable state commits MUST precede external side-effect cleanup**. Order: (1) commit status terminal; (2) best-effort cleanup of staging artefacts; (3) log orphan-tolerated event. Apply to MinIO, Redis staging keys, and any other transient store. |

(The supersede ordering bug is covered by the existing 2026-05-04 "concurrent worker + Reconciler" lock rule combined with the new commit-ordering rule above.)

---

## Pending Items

None. All 12 votes 6/6.
