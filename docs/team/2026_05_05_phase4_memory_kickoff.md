# Discussion: Phase 4 — Chat Agent Memory Service (Kickoff)

> Source: user directive 2026-05-05 — "plan phase 4: chat agent memory service with haystack 2.x"
> Date: 2026-05-05
> Mode: RAGENT Agent Team (6 voting members + Master)
> Scope: planning only — no Red phase yet. Output = `docs/00_spec.md` §3.8 + Phase 4 track in `docs/00_plan.md`.

---

## Master's Opening

**User-supplied requirements (verbatim):**
1. **Meta** lives in **MariaDB**.
2. **Short-term memory** = last **5 rounds**, kept in **Redis**.
3. **Long-term memory** = **distill** the rolling-out rounds into RAG-style vectors stored in **Elasticsearch**.
4. **Chat API**: add an **input flag to enable memory**; when enabled, chat combines (a) MariaDB meta + (b) Redis short-term + (c) ES long-term vector recall.
5. **Clear mechanism** required (具備 clear 機制).

**Triggered rules / context:**
- `00_journal.md` (Architecture) — auth/permission fields MUST NOT live on a retrieval index. **Memory recall must be permission-blind on ES; session ownership is enforced upstream of the retriever.** Same rule that drove B14.
- `00_journal.md` (Resilience) — every transient state and every async convergence task needs a Reconciler arm.
- `00_journal.md` (Capacity planning) — every "no cap" decision must include the worst-case multiplication.
- `00_journal.md` (Operator UX) — every new long-running async task needs a `@broker.task` registration on the canonical broker module (T0.10) + a composition-root construction site (T7.5a).
- `00_rule.md` §No physical FK / Mandatory indexing / UID rule / End-to-end UTC.
- `00_spec.md` §3.5 (Permission Layer is post-retrieval, OpenFGA-backed in P2) — Phase 4 inherits this; per-user memory recall is **session-scoped**, never cross-session.

**Topics this round:**
- **M1** Domain shape — three-layer (meta/short/long) split; what data lives where; **what is "one round"?**
- **M2** Chat API contract extension — request flag, response shape, session lifecycle, default behaviour when flag is off.
- **M3** Distillation trigger — when is a round eligible for ES long-term storage? Sync vs async (TaskIQ task `memory.distill`)?
- **M4** Clear mechanism — endpoint, cascade semantics, idempotency, Reconciler arm for orphans.
- **M5** Race / failure modes — concurrent same-session calls, distill-failure idempotency, Redis loss tolerance, ES freshness window.
- **M6** Phase ordering — does Phase 4 require Phase 2 (auth + Permission Layer) to be safe?

---

## Round 1 — Role Perspectives

### M1 · Domain shape (three layers)

- 🏗 **Architect**: [Pro] Three layers map cleanly to existing infra: MariaDB (durable session metadata), Redis (cheap rolling buffer — already in stack via B27), ES (vector recall — `chunks_v1` infra reused for `memory_v1`). [Con] Mixing knowledge retrieval (`chunks_v1`) with memory retrieval (`memory_v1`) on the same chat pipeline risks contaminating ranking. **Position**: keep `memory_v1` a **separate ES index** from `chunks_v1`; chat pipeline runs them as two retrieval branches and composes results in the prompt, not via RRF. **Define "round"** as one `(user_message, assistant_message)` tuple plus `round_id` (UUIDv7 → CHAR(26)) and `created_at`.
- ✅ **QA**: [Pro] Separating indices makes BDD testable per layer (S40 short-term injection, S41 long-term recall, S42 clear). [Con] If `memory_v1` mapping reuses `chunks_v1` shape we save ingest code; if it diverges we double the bootstrap surface. **Position**: keep mapping symmetric to `chunks_v1` (same `embedding` dims, same `icu_text` analyzer, same `bbq_hnsw`) so the ES bootstrap, drift test, and `/readyz` plugin probe (B26) cover it for free; only the **filter fields** differ (`session_id`, `user_id`, `kind`, `round_id`, no `source_app`/`source_workspace`).
- 🛡 **SRE**: [Pro] Redis short-term = list `memory:short:{session_id}` of JSON entries, capped at `MEMORY_SHORT_TERM_ROUNDS` (default 5) via `LPUSH + LTRIM 0, N-1`; sliding TTL `MEMORY_SHORT_TERM_TTL_SECONDS` (default 86400 = 24 h) refreshed each turn. [Con] Redis is not durable; an instance loss erases short-term memory mid-conversation. **Position**: explicitly mark Redis as **soft-state** in the spec (regenerable from MariaDB+ES on next turn — though degraded UX). Reuse the **rate-limiter Redis instance** (B27, second logical Redis) — broker Redis is for TaskIQ messages, not user data. Add `MEMORY_REDIS_*` topology env knobs that reuse the rate-limiter mode dispatch.
- 🔍 **Reviewer**: [Pro] Meta in MariaDB needs only what answers "is this session valid for this caller, and where is its distill cursor?" — `session_id`, `create_user`, `created_at`, `updated_at`, `round_count`, `distill_cursor` (last `round_id` distilled). No conversation text in MariaDB; full text lives in Redis (transient) and the distilled summary in ES (permanent). [Con] Storing every round's text in MariaDB would inflate the table and duplicate Redis. **Position**: MariaDB is **metadata-only**, never a transcript store.
- 📋 **PM**: [Pro] User-facing surface is "long-running chat that remembers things from earlier in the session". Three layers + clear mechanism cover the ask. **Position**: scope is bounded; reject any "global cross-session memory" extension in P4 (privacy + Permission Layer interaction).
- 💻 **Dev**: [Pro] Each layer maps to one file: `repositories/chat_session_repository.py` (MariaDB), `clients/memory_redis.py` (short-term), `plugins/memory_extractor.py` or direct ES retriever (long-term). Reuses existing `EmbeddingClient`, `Elasticsearch`, `MARIADB_DSN`, broker. **Position**: confirm. Three layers, three modules, one new ES index, one new TaskIQ task (`memory.distill`), one new MariaDB migration.

### M2 · Chat API extension

- 🏗 **Architect**: [Pro] Schema needs **two** new optional fields, not one: `enable_memory: bool = false` AND `session_id: str | null`. The flag alone cannot stitch turns across requests; without `session_id` the server has no way to find the right Redis key or ES filter. [Con] If `enable_memory=true` and `session_id` is absent, server must mint one and return it. **Position**: `ChatRequest` gains `enable_memory: bool` (default `false`) and `session_id: CHAR(26)?` (optional). Response `ChatResponse` gains `session_id: CHAR(26)?` echoed back (so the client can pin subsequent turns). Backwards-compatible with §3.4 — when both are absent/false, current P1 behaviour is unchanged.
- ✅ **QA**: [Pro] BDD: **S40** memory off ⇒ identical behaviour to §3.4 S6a (regression guard). **S41** memory on, no session ⇒ server mints `session_id`, returns it, persists meta row. **S42** memory on, valid session ⇒ short-term injected as message history, long-term injected as system context. **S43** memory on, session belongs to other `X-User-Id` ⇒ 403 (P1 OPEN still trusts the header for ownership audit). **S44** memory on, unknown `session_id` ⇒ 404 `MEMORY_SESSION_NOT_FOUND`. **Position**: those five BDD rows + filter validation (S45) are the acceptance contract.
- 🛡 **SRE**: [Pro] Same per-user rate limit (B31) applies — memory-enabled chat is more expensive (ES query + distill task), so the budget protects LLM cost. [Con] Distill task adds an LLM call per superseded round; cost projection: `MEMORY_SHORT_TERM_ROUNDS=5` ⇒ every 6th turn produces 1 distill LLM call. **Position**: add `MEMORY_DISTILL_LLM_MAX_TOKENS` (default 512) and a per-distill timeout `MEMORY_DISTILL_TIMEOUT_SECONDS` (default 60). Distill is **best-effort, async, retryable** — never blocks the chat response.
- 🔍 **Reviewer**: [Pro] Where in the message list does memory go? **Long-term recall** is system-context (prepended after the default system prompt as a `system` message containing recalled facts); **short-term history** is replayed as the actual `messages[]` between system and the latest user turn. The router still treats the **last `role:"user"` entry** as the retrieval query (§3.4.1 invariant). [Con] If we just blindly inject 5 rounds of history, prompt size balloons and `maxTokens` (4096) starves. **Position**: add `MEMORY_LONG_TERM_TOP_K` (default 5) and `MEMORY_LONG_TERM_MIN_SCORE` (default 0.3) to gate ES recall; document the prompt-shape ordering in §3.8.
- 📋 **PM**: [Pro] Default `enable_memory=false` keeps the existing chat surface stable — Phase 4 is purely additive. **Position**: confirm.
- 💻 **Dev**: [Pro] Field naming per B21: `session_id` is snake_case (resource id), `enable_memory` is snake_case (resource flag, not an LLM token knob). [Con] Should `enable_memory` be required when `session_id` is sent? **Position**: reject — sending `session_id` without `enable_memory=true` is a no-op (the server ignores it). 422 `MEMORY_FLAG_REQUIRED` only when client tries to set `session_id` AND `enable_memory=false` simultaneously (signal inconsistency).

### M3 · Distillation trigger

- 🏗 **Architect**: [Pro] Distill happens **on overflow**: every chat turn appends one round to Redis; if the list now exceeds `MEMORY_SHORT_TERM_ROUNDS`, the oldest round is `RPOP`-ped, packaged as `{round_id, user, assistant, created_at, session_id}`, and dispatched to TaskIQ task `memory.distill(payload)`. [Con] If LLM distill cost is unwanted, the cheap path is to embed the raw concatenated round (no LLM call). **Position**: ship **two-step distill** — cheap embed-only path is the *default* (`MEMORY_DISTILL_MODE=embed`), LLM-summarisation is opt-in (`MEMORY_DISTILL_MODE=summarize`). Both write the same `memory_v1` row shape; `kind` field disambiguates (`raw_round` | `summary`).
- ✅ **QA**: [Pro] BDD **S46 distill on overflow** — given 5 rounds in Redis, when the 6th turn completes, then `memory.distill` is enqueued exactly once with the popped round payload; ES `memory_v1` gains one row keyed by `round_id`. **S47 distill idempotent** — TaskIQ redelivery of `memory.distill` produces no duplicate ES row (PUT by `_id=round_id`, not `_id=auto`). **S48 distill failure does not break chat** — given the distill task raises, when the next chat turn arrives, then chat still succeeds; the failed round is retained in Redis until distill succeeds (Reconciler arm).
- 🛡 **SRE**: [Pro] Same `WORKER_MAX_ATTEMPTS=5` policy reused; `event=memory.distill_failed` log on terminal failure; metric `memory_distill_failed_total`. [Con] If distill keeps failing, Redis short-term grows unbounded (rounds queued for retry). **Position**: set a hard cap `MEMORY_REDIS_MAX_PENDING_ROUNDS` (default 50) — overflow beyond cap drops the oldest round and emits `event=memory.dropped reason=distill_backlog` (degraded but bounded; alerting fires).
- 🔍 **Reviewer**: [Pro] Distill payload identity = `round_id`. Redis pop must therefore happen **after** the chat response is sent to the client (so the client never blocks on the pop), but **before** the next turn's LPUSH (so ordering is preserved). FastAPI `BackgroundTasks` is the cleanest hook. [Con] If the API process crashes between response-flush and the post-response background hook, the round is durably persisted in Redis but never trimmed — Reconciler arm catches it (sweep `LLEN > MEMORY_SHORT_TERM_ROUNDS` sessions). **Position**: confirm — explicit Reconciler arm `memory_overflow` running every 5 min.
- 📋 **PM**: [Pro] Embed-only default keeps cost predictable (no extra LLM bill). Operators flip to `summarize` when they want richer recall. **Position**: confirm.
- 💻 **Dev**: [Pro] One worker file `src/ragent/workers/memory.py` exporting two `@broker.task`s: `memory.distill` (round → ES) and `memory.clear` (cascade clear, see M4). [Con] LLM-summarisation reuses `LLMClient.chat()` (T3.8) with a fixed system prompt `MEMORY_DISTILL_SYSTEM_PROMPT`. **Position**: confirm.

### M4 · Clear mechanism (具備 clear 機制)

- 🏗 **Architect**: [Pro] **`DELETE /chat/sessions/{session_id}`** — cascades across all three layers idempotently:
  1. **TX-A** (MariaDB) `acquire FOR UPDATE NOWAIT` on `chat_sessions` row → set `status='DELETING'` (commit).
  2. **Outside tx**: Redis `DEL memory:short:{session_id}` (idempotent).
  3. **Outside tx**: ES `delete_by_query` on `memory_v1` with `term: {session_id: ...}` (idempotent).
  4. **TX-B**: `DELETE FROM chat_sessions WHERE session_id=?` → 204.
  Mirror of §3.1 Delete flow — same locking/cascade discipline, same Reconciler resume on partial failure (`status='DELETING' AND updated_at < NOW() - 5 min`).
- ✅ **QA**: [Pro] BDD **S49 clear cascade** — given a session with 3 rounds in Redis + 2 rows in ES + 1 MariaDB row, when DELETE fires, then all three are removed and 204 is returned. **S50 clear idempotent** — re-DELETE returns 204; no errors. **S51 clear unknown session** — DELETE on unknown id returns 204 (idempotent semantics, matching `/ingest` S14).
- 🛡 **SRE**: [Pro] Reuse the `DELETING` state machine from `documents` (`UPLOADED→PENDING→READY→FAILED→DELETING`) — except sessions only have `ACTIVE` and `DELETING`. Two-state machine; valid transitions `{ACTIVE→DELETING}` only. [Con] Adds a second small state machine module. **Position**: factor `state_machine.py` to support multiple state-machine specs (or inline the 2-state check in repo).
- 🔍 **Reviewer**: [Pro] Authorization in P1 OPEN: `create_user` audit field on `chat_sessions` checked against `X-User-Id` header — unauth caller can technically delete another user's session. Documented as **P1 Accepted Risk** (same shape as ingest in P1 OPEN). [Con] Permission Layer (P2 §3.5) post-filters this when it ships. **Position**: ship the `create_user` check in P4 as audit-only; full enforcement deferred to whichever phase activates the Permission Layer.
- 📋 **PM**: [Pro] One endpoint, idempotent, predictable cost. Customer story: "user clicks 'forget this conversation' → all three layers cleared." **Position**: confirm.
- 💻 **Dev**: [Pro] Implementation = `memory_service.clear_session(session_id, requester_user_id)` orchestrating repo + Redis + ES + final repo delete. Cascade order matches §3.1. **Position**: confirm.

### M5 · Concurrency, failure modes, freshness

- 🏗 **Architect**: [Pro] Concurrent calls on **same session_id** from same user: Redis `LPUSH` is atomic per call but two parallel turns may interleave such that `messages[]` history doesn't reflect strict order. Acceptable for P4 (LLM is robust to mild reordering); rate-limiter (B31) caps parallelism. [Con] **Distill-failure idempotency**: TaskIQ at-least-once delivery means `memory.distill(round_id=X)` may fire twice. **Position**: ES `_id=round_id` ensures PUT-overwrite semantics; second delivery is a no-op.
- ✅ **QA**: [Pro] **S52 concurrent same-session** — given two parallel `enable_memory=true` calls on the same session, when both complete, then the Redis list contains both rounds (order unspecified) and `LLEN ≤ MEMORY_SHORT_TERM_ROUNDS + 1` is bounded by overflow capture. **S53 ES freshness** — distill writes are eventually visible (≤ refresh_interval=1s, B26 baseline); chat may not recall a just-distilled round on the immediately-next turn (acceptable, mirrors S32).
- 🛡 **SRE**: [Pro] Reconciler arms (extending §3.6):
  - **`UPLOADED`-equivalent**: any `chat_sessions` row in `DELETING > 5 min` → resume cascade (mirrors §3.6 R-DELETING).
  - **Memory overflow orphans**: every cycle, scan a bounded sample of session keys via `SCAN`; for any `LLEN > MEMORY_SHORT_TERM_ROUNDS`, dispatch `memory.distill` on overflow rounds.
  - **Distill-stuck rounds**: Redis-side counter `memory:distill_attempts:{round_id}` incremented per attempt; > 5 ⇒ drop with `event=memory.distill_failed`.
- 🔍 **Reviewer**: [Pro] No physical FK between `chat_sessions` and `memory_v1` (B14-style decoupling — ES owns its lifecycle). [Pro] Auth fields on `memory_v1`: **`user_id` is denormalised as a `keyword`** for filter scope (mirroring B29 `source_app`) — *but it is scope metadata, not auth* (lesson from `00_journal.md` Retrieval/ACL distinction). The retriever still applies `term: {session_id: ...}` as the primary filter; `user_id` is secondary scope for cross-session aggregation queries (none in P4, future-proofing only). **Position**: keep `user_id` on `memory_v1` for symmetry, document its non-auth role explicitly in spec §5.4.
- 📋 **PM**: [Pro] Failure modes covered without imposing user-visible cost. **Position**: confirm.
- 💻 **Dev**: [Pro] Idempotency by `round_id` is the exact same pattern as `chunk_id` in §3.1. Reuse mental model. **Position**: confirm.

### M6 · Phase ordering — does Phase 4 require Phase 2?

- 🏗 **Architect**: [Con] Without P2's Permission Layer, "is this caller the owner of session X?" is enforced only by trusting `X-User-Id` (P1 OPEN). Memory is more sensitive than knowledge chunks (it's literally the user's own conversation history) — leaking a session would be a real privacy bug, not just a search-result over-share. [Pro] However, P4 can ship the same-shape `create_user`-audit field today and gate hard enforcement on whichever phase activates auth. **Position**: P4 is **gated on at least P1 + P2** for production deployment; **dev/CI deployment is permitted under the same `RAGENT_AUTH_DISABLED=true AND RAGENT_ENV=dev` guard (T7.5)**. The startup guard should be extended in P4 with an extra refusal: "memory endpoints refuse to serve unless either auth is on (P2) or `RAGENT_ENV=dev`."
- ✅ **QA**: [Pro] Mirror P1's "auth-off-but-tagged" pattern; no new test infrastructure. **Position**: confirm.
- 🛡 **SRE**: [Pro] Production gate is enforceable; dev gate is unlocked. Same operational story as P1. [Con] If a careless operator runs P4 with `RAGENT_AUTH_DISABLED=true` in staging, memory leaks across users. **Position**: extend `bootstrap/guard.py` to refuse memory endpoints when `RAGENT_ENV=staging|prod` AND `RAGENT_AUTH_DISABLED=true`. Same module, one extra check.
- 🔍 **Reviewer**: [Pro] Document the P2 dependency explicitly in §3.8 — "memory endpoints are P2-gated for non-dev environments." [Con] Some users may want memory in P1 dev to ship demos; that path is covered by the dev guard. **Position**: confirm.
- 📋 **PM**: [Pro] Phase 4 plan can begin Red-phase development now (in dev), but production rollout waits on P2. Keeps engineering moving without blocking on the OpenFGA implementation. **Position**: confirm.
- 💻 **Dev**: [Pro] No code change required to wait for P2 — the guard module catches misconfiguration at boot. **Position**: confirm.

---

## Conflict Identification (Master)

| ID | Topic | Pending decision |
|---|---|---|
| **C-Mem-1** | M1 — separate `memory_v1` index vs reusing `chunks_v1` | Architect/QA prefer separate; blocking? |
| **C-Mem-2** | M2 — `enable_memory` semantics when `session_id` is absent | Auto-mint vs require client to mint |
| **C-Mem-3** | M3 — distill mode default | Embed-only (cheap) vs Summarize (richer) |
| **C-Mem-4** | M4 — clear endpoint cascade order | MariaDB→Redis→ES→MariaDB vs single TX |
| **C-Mem-5** | M5 — `user_id` denorm on `memory_v1` (auth vs scope distinction) | Include or omit |
| **C-Mem-6** | M6 — production gate on P2 | Soft (doc only) vs hard (guard refuses) |

---

## Voting

| Topic | Architect | QA | SRE | Reviewer | PM | Dev | Result |
|---|:---:|:---:|:---:|:---:|:---:|:---:|---|
| **C-Mem-1** Separate `memory_v1` index | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **6/6 Pass** |
| **C-Mem-2** Auto-mint `session_id` when absent + `enable_memory=true` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **6/6 Pass** |
| **C-Mem-3** Default distill mode = `embed` (LLM `summarize` opt-in) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **6/6 Pass** |
| **C-Mem-4** Clear cascade = TX-A→Redis→ES→TX-B (mirror §3.1 Delete) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **6/6 Pass** |
| **C-Mem-5** Include `user_id` keyword on `memory_v1` as **scope metadata** (not auth) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **6/6 Pass** |
| **C-Mem-6** Hard guard: memory endpoints refuse on `RAGENT_ENV ≠ dev AND RAGENT_AUTH_DISABLED=true` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **6/6 Pass** |

**Result: 6/6 Pass on all six topics.** No tie, no veto.

---

## Decision Summary

### Domain (§3.8 Chat Memory Service — to be added to `00_spec.md`)

- **Three layers, one chat surface:**
  - **Meta** (MariaDB) — `chat_sessions` table; `session_id CHAR(26) PK`, `create_user VARCHAR(64)`, `status ENUM('ACTIVE','DELETING')`, `round_count INT`, `last_distilled_round_id CHAR(26) NULL`, `created_at`, `updated_at`. Index `idx_create_user_session (create_user, session_id)` for "my sessions" lookups.
  - **Short-term** (Redis, rate-limiter instance, B27) — list `memory:short:{session_id}` of JSON entries `{round_id, role:user|assistant, content, created_at}`. Capped at `MEMORY_SHORT_TERM_ROUNDS` (default 5). Sliding TTL `MEMORY_SHORT_TERM_TTL_SECONDS` (default 86400). Soft state: regenerable from MariaDB+ES.
  - **Long-term** (ES `memory_v1`) — distilled rounds embedded with bge-m3 (`bbq_hnsw`, `cosine`, dims=1024 — symmetric with `chunks_v1`, B26). `_id = round_id` for idempotency. Filter fields: `session_id` (primary), `user_id` (scope, not auth — `00_journal.md` distinction).
- **One round** = one `(user_message, assistant_message)` tuple, identified by a server-minted `round_id` (UUIDv7 → CHAR(26)).
- **Permission-blind retrieval**: `memory_v1` carries no auth fields; session ownership is enforced by repository check on `chat_sessions.create_user` against `X-User-Id` (P1 OPEN) or JWT subject (P2+).

### Chat API extension (§3.4.1 update + §3.8.1 new)

- `ChatRequest` gains:
  - `enable_memory: bool` (default `false`).
  - `session_id: str | null` (CHAR(26); optional). When `enable_memory=true` and `session_id` is null, server mints a new id, persists meta, returns it in response.
- `ChatResponse` (§3.4.2) gains `session_id: str | null` (echoed when memory was used; else `null`).
- **Prompt assembly when memory enabled** (in order):
  1. Default system prompt (§3.4.1, B12).
  2. **Long-term recall context** as a second `system` message: top-K (`MEMORY_LONG_TERM_TOP_K`, default 5) `memory_v1` rows where `term: {session_id: ...}` AND vector similarity to last user message ≥ `MEMORY_LONG_TERM_MIN_SCORE` (default 0.3). Format: bullet list of recalled facts.
  3. **Short-term history** as actual `messages[]` entries between system and the latest turn (last `MEMORY_SHORT_TERM_ROUNDS` rounds, oldest-first).
  4. The new `messages[]` from the request (whose last `role:"user"` is also the **retrieval query** for both `chunks_v1` knowledge and `memory_v1` long-term).
- **Knowledge retrieval (§3.4) is unchanged** — `chunks_v1` BM25+vector still runs whenever `messages` has a user query, regardless of `enable_memory`. Memory is purely additive context.
- Validation:
  - 422 `MEMORY_SESSION_INVALID` when `session_id` is set but malformed (not 26-char Crockford Base32).
  - 422 `MEMORY_FLAG_INCONSISTENT` when `session_id` is set but `enable_memory=false`.
  - 404 `MEMORY_SESSION_NOT_FOUND` when `session_id` references no row (or `status='DELETING'`).
  - 403 `MEMORY_SESSION_FORBIDDEN` when `chat_sessions.create_user != X-User-Id`.

### Distillation (§3.8.2 new)

- **On every turn that completes (200 to client)**, a FastAPI `BackgroundTasks` hook:
  1. `LPUSH` the new round to Redis.
  2. `LTRIM` to `MEMORY_SHORT_TERM_ROUNDS`.
  3. If `LTRIM` evicted entries (i.e. list was already at capacity), `RPOP` the popped tail bytes — wait, simpler: use `LPUSH + LRANGE [N..-1]` to identify overflow and `LREM` them, then `LTRIM 0 N-1`. **Implementation detail deferred to T-Mem-7 Green phase**.
  4. For each evicted round, `kiq memory.distill({round_id, session_id, user_id, payload, mode})`.
- `mode = MEMORY_DISTILL_MODE` (default `embed`; `summarize` opt-in):
  - `embed`: payload is the concatenated `f"User: {user}\n\nAssistant: {assistant}"` text; embedded directly.
  - `summarize`: prepend an LLM call (`LLMClient.chat()`) with `MEMORY_DISTILL_SYSTEM_PROMPT`; resulting summary is the embedded text.
- Worker writes `memory_v1` row with `_id=round_id` (PUT semantics; idempotent under TaskIQ redelivery).

### Clear mechanism (§3.8.3 new)

- New endpoint **`DELETE /chat/sessions/{session_id}`** (router `chat.py` extension):
  - Auth: `X-User-Id`; ownership check against `chat_sessions.create_user`.
  - Cascade order (mirrors §3.1 Delete flow):
    1. **TX-A** `acquire FOR UPDATE NOWAIT` → set `status='DELETING'` → commit.
    2. Outside tx: Redis `DEL memory:short:{session_id}` (idempotent).
    3. Outside tx: ES `POST /memory_v1/_delete_by_query {term: {session_id: ...}}` (idempotent).
    4. **TX-B** `DELETE FROM chat_sessions WHERE session_id=?` → 204.
  - Idempotent: re-DELETE on already-cleared session returns 204 (mirrors `/ingest` S14).
  - Unknown session returns 204 (no error leakage on existence).

### Reconciler arms (§3.6 update)

- **`chat_sessions DELETING > MEMORY_RECONCILER_DELETING_STALE_SECONDS` (default 300)**: resume cascade clear idempotently.
- **Redis overflow orphans (memory.distill stuck)**: every Reconciler tick, sample N Redis keys matching `memory:short:*` via `SCAN`; for any `LLEN > MEMORY_SHORT_TERM_ROUNDS`, re-kiq `memory.distill` for the overflow tail. Bounded by `MEMORY_REDIS_MAX_PENDING_ROUNDS` (default 50); if a session exceeds the bound, the oldest tail is dropped with `event=memory.dropped reason=distill_backlog`.
- **`memory_v1` orphan vectors**: every cycle, run `SELECT session_id FROM chat_sessions WHERE status IN ('ACTIVE','DELETING')` and reconcile against ES `aggs cardinality session_id`; mismatched ids ⇒ ES `delete_by_query` (orphans from crashed clears).

### Phase ordering / guard (§3.8.4 new)

- Phase 4 may **enter Red-phase development immediately** (in parallel with Phase 1 finishing) — chat memory is independent infra.
- Phase 4 **production rollout requires Phase 2** (auth + Permission Layer) to enforce session ownership.
- **`bootstrap/guard.py` extended (T7.5 update)**: refuse to register memory endpoints when `(RAGENT_ENV ∈ {staging,prod}) AND (RAGENT_AUTH_DISABLED=true)`. Same boot-refuse pattern as P1 OPEN.

### Env vars (additions to §4.6)

| Variable | Default | Description |
|---|---|---|
| `MEMORY_SHORT_TERM_ROUNDS`            | `5`              | Redis short-term capacity per session. |
| `MEMORY_SHORT_TERM_TTL_SECONDS`       | `86400`          | Sliding TTL on `memory:short:{session_id}`. |
| `MEMORY_LONG_TERM_TOP_K`              | `5`              | ES `memory_v1` retriever top-K. |
| `MEMORY_LONG_TERM_MIN_SCORE`          | `0.3`            | Cosine-similarity floor for memory recall (below ⇒ omitted from prompt). |
| `MEMORY_DISTILL_MODE`                 | `embed`          | `embed` \| `summarize`. |
| `MEMORY_DISTILL_LLM_MAX_TOKENS`       | `512`            | Max tokens for `summarize` mode. |
| `MEMORY_DISTILL_TIMEOUT_SECONDS`      | `60`             | `memory.distill` task ceiling. |
| `MEMORY_DISTILL_SYSTEM_PROMPT`        | `Summarize the following exchange in 2-3 sentences, preserving any factual claims or commitments.` | `summarize` mode prompt. |
| `MEMORY_REDIS_MAX_PENDING_ROUNDS`     | `50`             | Hard cap on Redis short-term list length (overflow drops oldest). |
| `MEMORY_RECONCILER_DELETING_STALE_SECONDS` | `300`       | Stale `chat_sessions DELETING` re-sweep threshold. |
| `MEMORY_QUERY_TIMEOUT_SECONDS`        | `10`             | `memory_v1` ES query budget (chat-side). |
| `MEMORY_BULK_TIMEOUT_SECONDS`         | `60`             | `memory_v1` ES write budget (`memory.distill` worker). |

### Error code additions (§4.1.2)

| `error_code` | HTTP / Surface | When | Origin |
|---|---|---|---|
| `MEMORY_SESSION_INVALID`             | 422         | `session_id` malformed | Schema (T-Mem-3) |
| `MEMORY_FLAG_INCONSISTENT`           | 422         | `session_id` set with `enable_memory=false` | Schema (T-Mem-3) |
| `MEMORY_SESSION_NOT_FOUND`           | 404         | unknown `session_id` | Service (T-Mem-9) |
| `MEMORY_SESSION_FORBIDDEN`           | 403         | `create_user != X-User-Id` | Service (T-Mem-9) |
| `MEMORY_DISTILL_FAILED`              | log `event=memory.distill_failed` | `memory.distill > 5` attempts | Worker (T-Mem-13) |
| `MEMORY_BACKLOG_DROPPED`             | log `event=memory.dropped reason=distill_backlog` | `LLEN > MEMORY_REDIS_MAX_PENDING_ROUNDS` | Reconciler arm |

### Decision Log additions (§7)

- **B32** Three-layer split (MariaDB meta + Redis short + ES long) — chosen over: single-store (loses cost/latency tiering), in-memory (loses durability), graph DB (P3 concern).
- **B33** Separate `memory_v1` index from `chunks_v1` — chosen over: shared index with `kind` discriminator (couples lifecycles, complicates filters, B14-style risk).
- **B34** Distill default = embed-only — chosen over: summarize-default (LLM cost per overflow round), no-distill (long sessions silently lose recall).
- **B35** `enable_memory` flag default `false` — chosen over: opt-out (breaks back-compat with P1 chat surface).
- **B36** Memory production rollout gate = P2 auth — chosen over: ship-without-auth (privacy regression vs ingest), defer-to-P3 (blocks Phase 4 dev).

---

## Pending Items

None. All six topics passed 6/6 in Round 1; no second round required.

**Owner assignments for Red phase (Phase 4 track in `00_plan.md`):**

| Owner | Tracks |
|---|---|
| Dev | Schema / repository / Redis client / ES retriever / worker / router |
| QA | Unit + integration BDD (S40–S53) |
| SRE | Reconciler arms, alerting rules, env-var inventory drift test |
| Architect | §3.8 spec authoring, B32–B36 Decision Log rows |
| Reviewer | Cascade-order audit, idempotency audit, prompt-injection guard against retrieved memory text |
| PM | Acceptance metrics for Phase 4 quickstart (E2E "remembers across turns" smoke test) |

---

## Reflection (`00_journal.md` candidate row)

| Date | Domain | Issue Description | Root Cause | Actionable Guideline (Prevention) |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-05 | **Memory / Privacy** | Long-running chat sessions accumulate per-user state across three stores; without an explicit clear contract, privacy obligations (GDPR-style erasure, "forget this conversation" UX) cannot be honored cheaply. | Memory layers were considered separately during ideation; the cascade-clear contract was the missing cross-cutting concern. | **[Rule]** Every introduced memory tier (cache, short-term store, long-term vector index) MUST ship with a single-call clear endpoint that cascades across every tier in a documented order, mirrors the resource-delete flow's locking discipline (§3.1), and is covered by Reconciler arms for stuck-clear sessions. No memory tier is added without its clear path landing in the same plan track. |
