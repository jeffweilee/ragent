# Discussion: Phase 4 Memory Service — Round 3 Doc Review

> Source: user directive 2026-05-05 — Round-3 objective audit of edited Phase 4 docs on branch `claude/plan-memory-service-hsibf`.
> Date: 2026-05-05
> Mode: RAGENT Agent Team (6 voting members + Master), read-only — no file edits.
> Scope: verify the 5-point user objective is satisfied end-to-end across `00_spec.md`, `00_plan.md`, `00_journal.md`; flag any new defects introduced post Round-2.

---

## Master's Opening

**User objective recap (verbatim):**
1. Meta lives in MariaDB.
2. Short-term memory = last 5 rounds, kept in Redis.
3. Long-term memory = distill the rolling-out rounds into RAG-style vectors stored in Elasticsearch.
4. Chat API: input flag to enable memory; when enabled, combine MariaDB meta + Redis short-term + ES long-term.
5. Clear mechanism required (具備 clear 機制).

**Triggered rules / context:**
- `00_journal.md` 2026-05-05 *Planning hygiene* row — every planning track that produces spec deltas + plan-skeleton + wireframes MUST run a Round-2/Round-3 self-audit covering wire-diagram↔spec parity, count parity, one-Green-row-per-surface, ordering-loss tests, prompt-injection tests.
- `00_journal.md` 2026-05-05 *Memory / Privacy* row — every memory tier must ship clear-cascade + recall-sanitisation in the same plan track.
- `00_agent_team.md` step 6 — Track-Completion Gate (print full table; `[x]` only if backing test ran green; `[ ]` otherwise).
- `00_journal.md` 2026-05-04 *Process / TDD honesty* — `[x]` requires a recorded green run; skipped = `[ ]`.
- `CLAUDE.md` — Phase 1 baseline tech stack constraints; FastAPI / TaskIQ + Sentinel / Haystack 2.x / MariaDB / ES / OTEL; no new clients beyond P1 catalog without justification.

**Objective-trace map (where each user point lands in the docs):**

| Pt | Where it lands | Citation |
|---|---|---|
| **O1 Meta in MariaDB** | `chat_sessions` DDL (session_id PK, create_user, status, round_count, last_distilled_round_id, indexes); `ChatSessionRepository` plan rows. | `00_spec.md` §5.1 lines 754–773; §3.8 line 370 (table row); P4-A.1, P4-B.1/B.2 |
| **O2 5 rounds in Redis** | `MEMORY_SHORT_TERM_ROUNDS=5` default; "one round = one composite list entry"; overflow predicate `LLEN > N`; sliding TTL refresh. | `00_spec.md` §3.8 line 371; §3.8.2 lines 413–415; §4.6.9 line 700; S46/S52 lines 458/464; P4-B.3/B.3a/B.4 |
| **O3 Long-term distill into ES** | `memory_v1` mapping (1024-dim bbq_hnsw cosine; session_id+user_id+kind keyword); `_id=round_id`; `MEMORY_DISTILL_MODE` default `embed`, `summarize` opt-in; `memory.distill` worker. | `00_spec.md` §5.4 lines 837–878; §3.8.2 lines 418–425; B33/B34 lines 1024–1025; P4-A.2, P4-C.1–C.6 |
| **O4 Chat API flag combining 3 sources** | `enable_memory: bool=false` + `session_id` on ChatRequest/Response; prompt-assembly order (default sys → recall sys → short-term replay → new user); `chunks_v1` knowledge retrieval unchanged (additive). | `00_spec.md` §3.8.1 lines 380–407; §4.1 row 482; B35 line 1026; P4-E.1–E.7b |
| **O5 Clear mechanism** | `DELETE /chat/sessions/{id}` 204 idempotent; cascade TX-A → Redis DEL → ES `_delete_by_query` → TX-B; Reconciler `chat_sessions DELETING > 300 s` arm; S49–S51. | `00_spec.md` §3.8.3 lines 429–440; §4.1 row 482; §3.6 line 338; §3.8.4 line 444; S49–S51 lines 461–463; P4-F.1–F.4, P4-G.1/G.4 |

---

## Role Perspectives (One Pro / One Con per role per topic)

### O1 — Meta in MariaDB

- 🏗 Architect: [Pro] `chat_sessions` DDL at `00_spec.md` §5.1 lines 754–773 carries exactly the meta the kickoff specified — `session_id`/`create_user`/`status`/`round_count`/`last_distilled_round_id` with `idx_create_user_session` and `idx_status_updated`; transcript text stays out of MariaDB. [Con] `last_distilled_round_id` is declared but no Reconciler arm or repository row currently *consumes* it (the §3.8.4 orphan-vector arm uses `aggs cardinality session_id` instead). It is not strictly orphaned (audit value), but the connection is implicit.
- ✅ QA: [Pro] P4-B.1 covers `create / get / update_status / delete / list_for_clear` against `chat_sessions` (`00_plan.md` line 254). [Con] No row asserts `last_distilled_round_id` is updated by `memory.distill` Green code — if the cursor is meant to drive recovery, a test should pin it.
- 🛡 SRE: [Pro] Two-state machine `{ACTIVE→DELETING}` plus stale-DELETING sweep is durable + reconcilable (§3.8.4 line 444). [Con] None — meta-tier is the most conservative slice of the design.
- 🔍 Reviewer: [Pro] `create_user` documented as audit-only, not auth (lines 767–771 + B14 lineage in §3.8). Field-class taxonomy from journal Retrieval/ACL distinction is honored. [Con] None for the objective; meta is in MariaDB.
- 📋 PM: [Pro] "List my sessions" pattern is supported by `idx_create_user_session`. [Con] None.
- 💻 Dev: [Pro] `repositories/chat_session_repository.py` (P4-B.2) maps 1:1 onto the DDL. [Con] None.

### O2 — Short-term = last 5 rounds in Redis

- 🏗 Architect: [Pro] Round-entry semantics fixed: §3.8.2 line 413 explicitly says "one composite JSON entry per round … overflow predicate `LLEN > N`, not `N × 2`"; that contradiction (a Round-2 finding) is now resolved consistently. S46 line 458 asserts `LLEN == 5`; S52 line 464 asserts `LLEN ≤ N+1`. [Con] None — the lossy-op path (`LPUSH+LTRIM→RPOP`) is replaced by `LRANGE N -1` BEFORE `LTRIM 0 N-1` and is contract-tested in P4-B.3a (`00_plan.md` line 257).
- ✅ QA: [Pro] S46/S52 align with `MEMORY_SHORT_TERM_ROUNDS=5` (§4.6.9 line 700) and the `LRANGE → LTRIM` ordering test (P4-B.3a) prevents regression. [Con] None.
- 🛡 SRE: [Pro] Sliding TTL via `EXPIRE` each turn (§3.8.2 line 414); cap at `MEMORY_REDIS_MAX_PENDING_ROUNDS=50` with degraded-but-bounded drop event. [Con] None.
- 🔍 Reviewer: [Pro] One round = one entry resolves the ambiguity that drove R-Mem-1 in Round-2. [Con] None.
- 📋 PM: [Pro] User-visible behaviour ("remember the last 5 turns") matches default. [Con] None.
- 💻 Dev: [Pro] `MemoryRedisClient` (P4-B.4) reuses `_redis_topology` from P4-A.6 — the rate-limiter shared instance per B27. [Con] None.

### O3 — Long-term distill into ES

- 🏗 Architect: [Pro] `memory_v1` mapping (§5.4 lines 841–872) is symmetric to `chunks_v1` (icu_text, 1024-dim bbq_hnsw cosine), reusing bootstrap + drift test + ICU probe; `_id=round_id` for idempotency (§5.4 line 876). [Con] None.
- ✅ QA: [Pro] P4-C.1 (embed), P4-C.3 (summarize), P4-C.5 (idempotent), P4-C.6 (failure) cover S46/S47/S48 and both modes. [Con] None.
- 🛡 SRE: [Pro] §4.3 row "Memory Distill" lists `MEMORY_BULK_TIMEOUT_SECONDS` (line 563); `WORKER_MAX_ATTEMPTS` reused; `event=memory.distill_failed` + `memory_distill_failed_total` metric (§3.8.2 line 425). [Con] None.
- 🔍 Reviewer: [Pro] Embed-default vs summarize-opt-in matches B34 line 1025. `kind ∈ {embed_round, summary}` discriminator (§5.4 line 874) is consistent with both branches. [Con] None — the "raw_round" wording from kickoff M3 was tightened to `embed_round` in §5.4 + `00_plan.md` P4-C.1 (line 264) and matches.
- 📋 PM: [Pro] Default cost model is predictable (no extra LLM bill unless `summarize`). [Con] None.
- 💻 Dev: [Pro] `summarize` mode reuses existing `LLMClient.chat()` (T3.8) — §4.5 catalog (lines 575–581) needs no new client; reaffirmed in §4.3 row line 563. [Con] None.

### O4 — Chat API flag combining all three sources

- 🏗 Architect: [Pro] §3.8.1 lines 400–406 specifies the strict prompt-assembly order: default system → long-term recall (fenced + sanitised) → short-term replay → new user message. The four-layer combination (MariaDB ownership check → Redis history → ES recall → new turn) is a deterministic pipeline. [Con] None — invariant "last `role:user` is the retrieval query" preserved (line 405).
- ✅ QA: [Pro] S40 (`00_spec.md` line 452) requires bit-equivalence with §3.4 S6a when memory is off; P4-E.3 (`00_plan.md` line 287) operationalises this as the byte-equivalent regression test. P4-E.6a (line 291) extends the same invariant to `/chat/stream`'s terminal `done` event. [Con] None — Round-2 R-Mem-3 (separate stream Green row) and R-Mem-4 (byte-equivalent regression) both folded.
- 🛡 SRE: [Pro] Memory-recall ES query has its own budget `MEMORY_QUERY_TIMEOUT_SECONDS=10` (§4.6.9 line 710 + §4.3 line 562). Rate-limit dep reused (P4-E.7a line 292). [Con] None.
- 🔍 Reviewer: [Pro] Sanitisation contract (strip role tokens, 1 KiB cap, fenced block) is a Decision row (§3.8.1 line 403) AND a test row (P4-D.3a line 278) — closes the prompt-injection vector explicitly noted in journal 2026-05-05 *Memory / Privacy*. [Con] None.
- 📋 PM: [Pro] Flag default `false` (B35 line 1026) preserves P1 chat surface — additive only. [Con] None.
- 💻 Dev: [Pro] Two Green rows P4-E.7a (`/chat`) + P4-E.7b (`/chat/stream`) — no conflation. [Con] None.

### O5 — Clear mechanism

- 🏗 Architect: [Pro] §3.8.3 cascade order (TX-A → Redis DEL → ES `_delete_by_query` → TX-B) mirrors §3.1 Delete locking discipline. Endpoint row at §4.1 line 482 (`DELETE /chat/sessions/{id}` `204` idempotent). [Con] None.
- ✅ QA: [Pro] S49 cascade, S50 idempotent re-DELETE, S51 unknown-id 204 no-existence-leak — all three plus P4-F.1/F.2 (`00_plan.md` lines 299–300). [Con] None.
- 🛡 SRE: [Pro] Reconciler arm `chat_sessions DELETING > MEMORY_RECONCILER_DELETING_STALE_SECONDS=300` (§3.6 line 338, §3.8.4 line 444) + P4-G.1 (line 308) covers stuck-clear sessions. Matches journal 2026-05-05 Memory/Privacy rule. [Con] None.
- 🔍 Reviewer: [Pro] Idempotent semantics matches `/ingest` S14 lineage (§3.8.3 line 439) — consistent project convention. [Con] None.
- 📋 PM: [Pro] User-visible "forget this conversation" is a single endpoint, 204, no error leakage. [Con] None.
- 💻 Dev: [Pro] `services/memory_service.py::clear_session` orchestration row at P4-F.3 (line 301). [Con] None.

### O6 — Cross-cutting integrity audit (Round-3 specific)

Sub-checks against the seven items the user enumerated:

- **(a) Phase 1 DoD ↔ S40–S54 plan-row coverage.** §3.X DoD `00_plan.md` line 199 says "Every BDD scenario in `00_spec.md` §3.X has a corresponding plan row whose test path matches." Coverage map: S40→P4-E.3 byte-equivalent test (line 287); S41→P4-E.4 (288); S42→P4-E.5 (289); S43+S44→P4-E.6 (290); S45→P4-E.1 (285); S46→P4-C.1 + P4-B.3a (264, 257); S47→P4-C.5 (268); S48→P4-C.6 (269); S49→P4-F.1 (299); S50+S51→P4-F.2 (300); S52→P4-H.1 (317); S53→P4-H.2 (318); S54→P4-H.3 (319). **All 15 scenarios mapped.**
- **(b) §4.3 "Chat Memory recall" test paths.** Catalog row line 562 cites `tests/unit/test_memory_recall.py` (P4-D.1) and `tests/unit/test_memory_recall_sanitization.py` (P4-D.3a). Both filenames match plan rows verbatim (lines 275 + 278). Pass.
- **(c) `MemoryRedisClient` reuses `_redis_topology` (P4-A.6) without breaking T3.13/T3.14.** P4-A.6 explicitly says "No behaviour change; verified by re-running T3.13/T3.14 unit tests green" (line 247). Pass.
- **(d) §3.8.2 `summarize` calls `LLMClient.chat()` while §4.5 still says LLMClient is P1.** §4.5 line 579 lists `LLMClient` as P1; §3.8.2 line 422 reuses `LLMClient.chat()` — no new client needed. §4.3 row 563 cites the same. Pass.
- **(e) `MEMORY_QUERY_TIMEOUT_SECONDS` and `MEMORY_BULK_TIMEOUT_SECONDS` consumption.** Both wired in §4.3 catalog row 562 (Memory recall · `MEMORY_QUERY_TIMEOUT_SECONDS`) and 563 (Memory Distill · `MEMORY_BULK_TIMEOUT_SECONDS`). Neither is orphaned. Pass.
- **(f) Journal "Planning hygiene" rule mandates a step Phase 4 itself didn't run.** The journal rule (line 42) requires Round-2 self-audit BEFORE any Red row; Round-2 produced 12 R-Mem fixes and Round-3 is the convergence audit. P4 DoD line 341 says "track-completion audit per `00_agent_team.md` step 6 prints the table" — the audit gate IS in the DoD. Pass.
- **(g) `MEMORY_REDIS_MAX_PENDING_ROUNDS` ↔ `MEMORY_BACKLOG_DROPPED` wiring.** Var defined §4.6.9 line 708; consumed in §3.6 line 339 + §3.8.2 line 427 + §3.8.4 implicit; error code `MEMORY_BACKLOG_DROPPED` defined §4.1.2 line 539 with origin `Reconciler P4-G.4`; plan row P4-G.2 line 309 explicitly tests "if LLEN > `MEMORY_REDIS_MAX_PENDING_ROUNDS`, oldest tail is dropped with `event=memory.dropped`". Pass.

**Six-role response on O6:**

- 🏗 Architect: [Pro] Every cross-reference resolves; B32–B36 cross-cited from §3.8 and §7. [Con] None new.
- ✅ QA: [Pro] All 15 BDD scenarios have a uniquely-identified plan row with test path. [Con] None new.
- 🛡 SRE: [Pro] Backlog-drop error code, capacity rule, and Reconciler arm form a closed loop. [Con] None new.
- 🔍 Reviewer: [Pro] No spec ↔ plan ↔ catalog drift detected; `_redis_topology` extraction (P4-A.6) is justified by reuse, not speculation. [Con] None new.
- 📋 PM: [Pro] Operator-facing quickstart row in P4 DoD (line 347 — "recalls 'teal' by turn 8 and clears with 204") is a verifiable acceptance test. [Con] None new.
- 💻 Dev: [Pro] No new clients required (LLMClient + EmbeddingClient + ES + Redis + MariaDB are all in P1 catalog already). [Con] None new.

---

## Conflict Identification (Master)

| ID | Topic | Status |
|---|---|---|
| O1 Meta in MariaDB | Resolved — `chat_sessions` DDL + repository plan rows present | ✓ |
| O2 Redis 5 rounds | Resolved — round-entry semantics + `LRANGE → LTRIM` ordering test | ✓ |
| O3 ES distill | Resolved — `memory_v1` mapping + idempotent PUT + `kind` discriminator + worker tracks | ✓ |
| O4 Chat API flag combining | Resolved — schema + prompt-assembly + S40 byte-equivalent + `/chat/stream` `done` event | ✓ |
| O5 Clear mechanism | Resolved — DELETE endpoint + 4-step cascade + idempotency + Reconciler arm | ✓ |
| O6 Cross-cutting integrity | Resolved — all seven sub-checks pass | ✓ |

No new defects identified. No R-IDs proposed.

---

## Voting

### M-O1 — "The updated docs satisfy the 5-point user objective end-to-end without further edits."

| Role | Vote |
|---|:---:|
| 🏗 Architect | ✅ Approve |
| ✅ QA | ✅ Approve |
| 🛡 SRE | ✅ Approve |
| 🔍 Reviewer | ✅ Approve |
| 📋 PM | ✅ Approve |
| 💻 Dev | ✅ Approve |

**M-O1 Result: 6/6 Approve — PASS.** No tie, no veto, Master need not cast.

### M-O2 — Conditional pass

Not invoked (M-O1 passed unconditionally).

---

## Decision Summary

The Round-3 objective audit confirms that the three edited artefacts on branch `claude/plan-memory-service-hsibf` satisfy the 5-point user objective end-to-end without further doc edits. Specifically:

- **O1 (Meta → MariaDB):** `chat_sessions` DDL at `00_spec.md` §5.1 lines 754–773 with `idx_create_user_session` + `idx_status_updated`; `ChatSessionRepository` plan rows P4-B.1/B.2.
- **O2 (Short-term → Redis, 5 rounds):** `MEMORY_SHORT_TERM_ROUNDS=5` (§4.6.9 line 700); composite-entry round-encoding (§3.8.2 line 413); `LLEN > N` overflow predicate (line 415); `LRANGE → LTRIM` lossless ordering verified by P4-B.3a.
- **O3 (Long-term → ES distill):** `memory_v1` mapping (§5.4 lines 841–872) + `_id=round_id` idempotency (line 876); `MEMORY_DISTILL_MODE=embed` default with `summarize` opt-in (§3.8.2 line 420 + B34); P4-C.1–C.6 covers both modes + idempotency + failure.
- **O4 (Chat API flag combining all three):** `enable_memory: bool=false` + `session_id` extension (§3.8.1 lines 380–392); strict prompt-assembly order (lines 400–406); S40 byte-equivalent regression (P4-E.3); `/chat/stream` `done` event carries `session_id` (P4-E.6a/E.7b).
- **O5 (Clear mechanism):** `DELETE /chat/sessions/{id}` 204 idempotent (§4.1 line 482); 4-step cascade TX-A → Redis DEL → ES `_delete_by_query` → TX-B (§3.8.3 lines 434–438); Reconciler arm `chat_sessions DELETING > 300 s` (§3.6 line 338 + §3.8.4 line 444); P4-F + P4-G.1.
- **O6 (Cross-cutting integrity):** all seven Round-3 sub-checks pass — BDD-to-plan parity (15/15), pipeline-catalog test paths match, `_redis_topology` extraction preserves T3.13/T3.14, no new third-party client required, both new timeout vars consumed in §4.3, P4 DoD includes the track-completion gate, `MEMORY_BACKLOG_DROPPED` is wired against `MEMORY_REDIS_MAX_PENDING_ROUNDS`.

**Accepted trade-offs (carried from Round-1, unchanged):**
- B36 production rollout gate on P2 — boot guard refusal (`bootstrap/guard.py` extension) is a startup-fail-closed; dev-only enforcement is unlocked under the existing P1 OPEN guard.
- B34 distill default = embed-only — narrower recall fidelity in exchange for predictable cost; opt-in summarize available.
- Memory production endpoints will not enable until Phase 2 (`RAGENT_AUTH_DISABLED=false`); Red-phase development may begin in parallel with Phase 1 finishing (per user directive 2026-05-05 captured at `00_plan.md` line 234).

**Fold-in list of new R-IDs:** None. The Round-2 R-Mem-1 through R-Mem-12 are already folded into the spec/plan; no Round-3 R-IDs are needed.

---

## Pending Items

**None.** All six topics passed 6/6 Approve in Round 3; the Phase 4 documentation set (`00_spec.md` §3.6/§3.8/§4.1/§4.1.2/§4.3/§4.6.9/§5.1/§5.4/§7 B32–B36 + `00_plan.md` Phase 4 + `00_journal.md` two new rows) is internally consistent and satisfies the 5-point user objective. Phase 4 Red-phase development may begin against the current artefacts.

---

## Reflection (`00_journal.md` candidate row)

| Date | Domain | Issue Description | Root Cause | Actionable Guideline (Prevention) |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-05 | **Planning hygiene (convergence)** | A Round-3 objective audit against the originating user requirements (the 5-point list) found zero new defects after Round-2 had folded 12 R-IDs — but the audit only had value because it traced **each user requirement to a citable spec/plan/test triple** rather than re-reading the spec linearly. A linear re-read would have surfaced "everything looks fine" without verifying that the user's *intent* lands in a backing test. | The team's natural Round-3 instinct is to re-validate internal consistency; the user's objective is external, and linear re-reads optimise for the former. | **[Rule]** Round-3 audits MUST execute an explicit objective-trace step: enumerate the originating user requirements (numbered list, verbatim), and for each requirement produce a (spec section, plan track row, BDD scenario, test file path) quadruple. A requirement without all four columns populated is a defect, even if the docs are internally consistent. The objective-trace map is the first artefact in the Round-3 review file, before any role debate. |
