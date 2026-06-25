# 00_plan.md — Master TDD Implementation Checklist

> Source: `docs/00_spec.md` · Workflow: `CLAUDE.md §THE TDD WORKFLOW`
> Each `[ ]` = one Red→Green→Refactor cycle; each cycle = one (or two) commits.
> Completed and descoped tracks are archived in [`docs/00_plan_done.md`](00_plan_done.md).

## Status legend
- `[x]` delivered
- `[ ]` TODO
- `[~]` descoped / deferred (not doing in this cycle)

---

## Track T-ICU — ICU Analyzer Convergence

**Counter: 完成 3 / 未完成 1 / descope 0**

| # | Category | Task | Commit | Status | Owner |
|---|---|---|:-:|:-:|---|
| T-ICU.1 | Structural | • **Achieve:** Reconcile spec §5.2 with B26.<br>• **Deliver:** Updated spec section and ES mapping alignment. | eb7480a | [x] | Dev |
| T-ICU.2 | Red | • **Achieve:** Pin ICU analyzer in prod mapping; pin standard analyzer in test mapping.<br>• **Deliver:** `tests/integration/test_icu_analyzer.py` — prod mapping uses `icu_text`; test mapping uses `standard`. | 1cc791d | [x] | QA |
| T-ICU.3 | Green | • **Achieve:** Implement env-driven mapping dir + commit two mapping files.<br>• **Deliver:** `resources/es/mappings/` with prod and test variants; `ES_MAPPING_DIR` env var. | 1cc791d | [x] | Dev |
| T-ICU.4 | Acceptance | • **Achieve:** Manual / staging smoke test for CJK BM25 (S36 coverage gap from B42).<br>• **Deliver:** Documented procedure: operator runs `Dockerfile.es-test` ES, applies prod mapping, indexes a `"產品規格"` doc, verifies `_analyze` tokenises into `["產品", "規格"]` and BM25 query recalls. Tracked as a release-gate manual step; not blocking pre-commit.<br>• **Success criteria:** Ops team runs the procedure on a staging cluster; `_analyze` returns `["產品", "規格"]`; BM25 query confirms recall; result recorded in a dated note and the release-gate checklist row is updated. | T-ICU.3 | [ ] | Ops |

---

## Track T-CAv3R — ChatAgent v3 Resumable Stream (Redis Stream buffer)

> Source: 2026-06-22 design session. Goal: a client that refreshes / disconnects
> mid-generation can rejoin the **same** in-flight run and receive the rest of the
> answer — not just the already-rendered prefix.
>
> **Locked decisions:**
> - **Full decoupling**: a background producer tees the run into a Redis Stream
>   independent of the client connection, so generation completes even if the
>   client leaves (within the TTL). Producer is an in-process daemon thread (option
>   A), not a TaskIQ worker — minimal change, sufficient for the refresh case.
> - Scope is **`/chatagent/v3` only** (the path the mco-clean `@twp/ai` data layer
>   already drives via `/twp/v1/run`). Core `/chat/v1/stream` is untouched.
> - Stream key is **owner-scoped** (`chatstream:{user}:{thread}:{run}`) so a run
>   cannot be reconnected by guessing its `runId`.
> - Resume uses **SSE `Last-Event-ID`** (exclusive cursor) over `fetch`+ReadableStream
>   (header auth precludes `EventSource`). Backend just emits `id:` lines + reads the
>   header. TTL 5 min; expired/unknown buffer → `RUN_ERROR(CHATAGENT_STREAM_EXPIRED)`,
>   client falls back to `GET /chatagent/v3/session`.

**Counter: 完成 6 / 未完成 1 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv3R.1 | Red+Green | • **Achieve:** `ChatStreamStore` — owner-scoped key; `XADD` append; `XRANGE` `read_after` (Last-Event-ID exclusive); `eos` sentinel + TTL on `mark_done`; `SET NX` single-producer `try_start`; `exists`; Sentinel-aware `from_env`.<br>• **Deliver:** `src/ragent/clients/chat_stream_store.py`; `tests/unit/test_chat_stream_store.py` (fakeredis). | [x] | Dev |
| T-CAv3R.2 | Red+Green | • **Achieve:** New error code `CHATAGENT_STREAM_EXPIRED` (SSE-error only).<br>• **Deliver:** `src/ragent/errors/codes.py`; `docs/spec/error_codes.md`. | [x] | Dev |
| T-CAv3R.3 | Red+Green | • **Achieve:** v3 POST decoupled producer/consumer — background daemon-thread producer tees `ADKAgent.run` into the buffer (single-producer lock); response consumes the buffer, attaching each entry id as the SSE `id:`. No store wired → legacy connection-bound stream. Event sequence unchanged.<br>• **Deliver:** `routers/chatagent_v3.py` (`_spawn_producer`, `_consume_stream`); `tests/unit/test_chatagent_v3_router.py`; `tests/helpers.py` (`parse_sse_events` tolerates `id:`, `parse_sse_ids`). | [x] | Dev |
| T-CAv3R.4 | Red+Green | • **Achieve:** `GET /chatagent/v3/reconnect?thread_id&run_id` — `Last-Event-ID` (exclusive) resume; missing/other-owner buffer → `RUN_ERROR(CHATAGENT_STREAM_EXPIRED)`.<br>• **Deliver:** `routers/chatagent_v3.py` reconnect route + `_stream_expired`; `tests/unit/test_chatagent_v3_router.py` (resume / expired / owner-scoped). | [x] | Dev |
| T-CAv3R.W1 | Behavioral | • **Achieve:** Wire the store into the composition root + v3 registration (built only when `CHATAGENT_API_URL` is set); add stream env vars.<br>• **Deliver:** `bootstrap/composition.py` (`chat_stream_store` field + `from_env`); `bootstrap/app.py` v3 registration; `docs/spec/env_vars.md`. | [x] | Dev |
| T-CAv3R.D1 | Structural | • **Achieve:** Document the resumable-stream contract + reconnect endpoint.<br>• **Deliver:** `docs/spec/chatagent_v3.md` §3.4.7 resumable-stream block; `docs/00_spec.md` pointer if needed. | [x] | Dev |
| T-CAv3R.FE1 | Red+Green | • **Achieve:** mco-clean `@twp/ai` persists `{threadId, runId, lastEventId}` for an in-flight run, reconnects via `GET /chatagent/v3/reconnect` (sends `Last-Event-ID` header) on remount, clears the marker on terminal frame, and falls back to `GET /chatagent/v3/session` on `CHATAGENT_STREAM_EXPIRED`. **(frontend — out of this backend cycle)** | [ ] | Dev |

### Sub-track T-CAv3R2 — server-authoritative reconnect (robustness follow-up)

> Source: 2026-06-24 design review. The merged reconnect trusted a **client-supplied
> `run_id`**, which can be stale (another tab/device started a newer run) and would
> resurrect an old, already-persisted turn out of order. And the live stream never
> carries the user turn, so a refresh mid-generation showed the answer with no
> question. Fix: make the **server** the authority on "the thread's current run",
> and stash the user turn so reconnect can replay it — no reliance on client state.
>
> **Locked decisions:**
> - reconnect takes **`thread_id` only**; resolves the run from a per-thread
>   `chatcurrent:{user}:{thread}` pointer (set on POST). Per-user → owner-scoped.
> - The run's user turn is stashed (`…:{run}:user`) and replayed as a new
>   `USER_MESSAGE` twp-ai event on a from-start reconnect (not on incremental).
> - This avoids any cross-source (session vs buffer) dedup on the FE: the FE shows
>   session history + (gated) the one in-flight turn from reconnect, never merging.

**Counter: 完成 4 / 未完成 1 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv3R2.1 | Red+Green | • **Achieve:** `ChatStreamStore` — per-thread current-run pointer (`set_current`/`get_current`, distinct `chatcurrent:` prefix so a `run_id` named `current` cannot collide) + user-turn stash (`stash_user_input`/`get_user_input`); both fail-soft on Redis error.<br>• **Deliver:** `src/ragent/clients/chat_stream_store.py`; `tests/unit/test_chat_stream_store.py`. | [x] | Dev |
| T-CAv3R2.2 | Red+Green | • **Achieve:** `USER_MESSAGE` twp-ai event (`{messageId, content, role:"user"}`) added to the event union.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/events.py`; `packages/twp-ai/tests/test_twp_protocol.py`. | [x] | Dev |
| T-CAv3R2.3 | Red+Green | • **Achieve:** v3 POST records the current-run pointer + stashes the last user-turn text (electing producer only). | [x] | Dev |
| T-CAv3R2.4 | Red+Green | • **Achieve:** `GET /chatagent/v3/reconnect` drops the `run_id` param; resolves the current run server-side; emits the stashed `USER_MESSAGE` first on a from-start replay; unknown/other-user thread → `CHATAGENT_STREAM_EXPIRED`.<br>• **Deliver:** `routers/chatagent_v3.py` (`_reconnect_stream`, `_last_user_text`); `tests/unit/test_chatagent_v3_router.py` (current-run resolve, latest-not-stale, user-turn replay); `docs/spec/chatagent_v3.md` §3.4.7, `docs/API.md`. | [x] | Dev |
| T-CAv3R2.FE1 | Red+Green | • **Achieve:** mco-clean reconnect flow — on mount load `GET /session`, then `GET /reconnect?thread_id` (no client run_id); render the leading `USER_MESSAGE`; gate against session by last-user-turn content to avoid the grace-window overlap. **(frontend — out of this backend cycle)** | [ ] | Dev |

---

## Track T-CAv3S — ChatAgent v3 Session History (twp-ai roles + hidden filtering)

> Source: 2026-06-11 design session. Two linked changes driven by the upstream
> keeping conversation memory by `session` and persisting every turn verbatim:
> (1) the `<hidden>` context/state preamble we prepend leaks back out through the
> read paths, and (2) the session history must be relabelled to twp-ai roles so the
> mco-clean `@twp/ai` data layer renders it like the v3 stream.
>
> **Locked decisions:**
> - Hidden filtering is **outbound only** (strip on surfaced content); no inbound
>   sanitization of client-supplied messages this cycle.
> - The upgraded session surface lives at **`/chatagent/v3/session*`** (the twp-ai
>   protocol family) — `/chatagent/v2` is already the raw-proxy POST. `/chatagent/v1`
>   session routes stay live for cutover.
> - Role mapping reuses the **same `node_to_role` rule as the v3 stream**: `user`→`user`,
>   `tool`→`tool`, assistant+`planner`→`reasoning`, other assistant nodes→`assistant`.

**Counter: 完成 14 / 未完成 1 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv3S.1 | Structural | • **Achieve:** Extract the upstream-role classifier into a single source of truth shared by the v3 stream and the session mapper.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/roles.py::node_to_role` + `REASONING_NODE`; `agents/adk.py` rewired to it; `packages/twp-ai/tests/test_roles.py`. Existing ADKAgent tests stay green (no behavior change). | [x] | Dev |
| T-CAv3S.2 | Red+Green | • **Achieve:** Strip `<hidden>…</hidden>` from surfaced content; no-op (no trimming) when no block is present. Applied **only** on the session-history read — the v3 stream carries the agent's own deltas, never the user turn's preamble, so it is not stripped there.<br>• **Deliver:** `src/ragent/utility/hidden.py::strip_hidden`; `tests/unit/test_hidden.py`; consumed by `services/chatagent_session.py`. | [x] | Dev |
| T-CAv3S.3 | Red+Green | • **Achieve:** Map upstream session history to twp-ai message shape `{id, role, content}` — role via `node_to_role`, content via `strip_hidden`; envelope preserved; payload without a `messages` list passes through.<br>• **Deliver:** `src/ragent/services/chatagent_session.py::map_session_payload`; `tests/unit/test_chatagent_session_mapper.py`. | [x] | Dev |
| T-CAv3S.4 | Structural | • **Achieve:** Extract the shared session-proxy plumbing (threadpool dispatch, status check, timeout→504/error→502 mapping, optional response `transform`) so v1 and v3 share one copy.<br>• **Deliver:** `src/ragent/routers/_chatagent_proxy.py`; `routers/chatagent.py` (v1) refactored to delegate. v1 unit + integration tests stay green. | [x] | Dev |
| T-CAv3S.5 | Red+Green | • **Achieve:** Add `/chatagent/v3` session surface — `GET /sessionList` (proxied), `GET /session` (reshaped via `map_session_payload`), `PUT`/`DELETE /session` (proxied).<br>• **Deliver:** `routers/chatagent_v3.py` session routes; `tests/integration/test_chatagent_v3_endpoint.py` — role mapping + hidden strip on GET, sessionList passthrough. | [x] | Dev |
| T-CAv3S.W1 | Behavioral | • **Achieve:** Wire the two session upstream URLs into the v3 router registration.<br>• **Deliver:** `bootstrap/app.py` v3 registration passes `chatagent_sessionlist_api_url`/`chatagent_session_api_url`. | [x] | Dev |
| T-CAv3S.D1 | Structural | • **Achieve:** Document the outbound hidden-strip rule and the v3 session surface.<br>• **Deliver:** `docs/00_spec.md` §3.4.7 (outbound strip bullet) + new §3.4.8 (v3 session management). | [x] | Dev |
| T-CAv3S.FE1 | Red+Green | • **Achieve:** mco-clean `@twp/ai` data layer consumes `/chatagent/v3/session`, preserving `reasoning`/`tool` roles (panel UI unchanged).<br>• **Deliver:** mco-clean `packages/ai` session client + mapper + types.<br>• **Success criteria:** `packages/ai` session client calls `/chatagent/v3/session*`; `reasoning` and `tool` roles round-trip correctly through the data layer; panel UI renders reasoning and tool turns without regression; unit tests in `packages/ai` pass. | [ ] | Dev |
| T-CAv3S.BC1 | Red+Green | • **Achieve:** Backward compat (PR #175 review) — the session read also strips the legacy bare `<context>…</context>` block that pre-v3 sessions carry, not just `<hidden>`. `strip_hidden` generalized + renamed `strip_machine_context`.<br>• **Deliver:** `src/ragent/utility/hidden.py::strip_machine_context`; `tests/unit/test_hidden.py` legacy-context cases; `tests/unit/test_chatagent_session_mapper.py` legacy case; `docs/00_spec.md` §3.4.8. | [x] | Dev |
| T-CAv3S.B2 | Red+Green | • **Achieve:** Session-id ownership (Model B) — `RunAgentInput.thread_id` optional; v3 mints `new_id()` when absent (single owner = ragent; upstream never mints), echoes it in `RUN_STARTED`; native `/twp/v1/run` defaults a uuid so RUN_STARTED is never null. Document `messages[].id` as client-optimistic / ignored.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/schemas.py` (optional `thread_id` + `Message.id` comment); `app.py` native default; `routers/chatagent_v3.py` mint; `tests/unit/test_chatagent_v3_router.py` + `packages/twp-ai/tests/test_twp_protocol.py`; `docs/00_spec.md` §3.4.7. | [x] | Dev |
| T-CAv3S.BC2 | Red+Green | • **Achieve:** Strip the machine-context wrapper from `sessionName` too — the upstream derives the title from the first user turn (which carries the block), so it leaked into the session list and session GET title.<br>• **Deliver:** `services/chatagent_session.py` (`_strip_session_name`, `map_session_list_payload`, `sessionName` stripped in `map_session_payload`); `routers/chatagent_v3.py` sessionList `transform`; `tests/unit/test_chatagent_session_mapper.py` + `tests/integration/test_chatagent_v3_endpoint.py`; `docs/00_spec.md` §3.4.8. | [x] | Dev |
| T-CAv3S.BC3 | Red+Green | • **Achieve:** Decode JSON-double-encoded `content`/`sessionName` before the wrapper strip — the upstream stores some values as a quoted string with literal `\n` escapes, so a leading `"` and `\n\n` survived the strip (`"\n\n<message>"`).<br>• **Deliver:** `services/chatagent_session.py` (`_unwrap_json_string` + `_clean_text`, applied to content + sessionName); `tests/unit/test_chatagent_session_mapper.py` double-encoded cases. | [x] | Dev |
| T-CAv3S.HITL1 | Red+Green | • **Achieve:** Human-in-the-loop interrupt outcome — an upstream `humanInTheLoopMeta.isInterrupt` no longer emits a standalone TEXT_MESSAGE; instead the run ends with `RUN_FINISHED.outcome={type:"interrupt", interrupts:[{id,reason,message?,toolCallId?,metadata?}]}` (success outcome otherwise). The interrupt message's own content / tool-call deltas still stream. `outcome` is emitted only on the v3 ADK path (native `/twp/v1` omits it). **PR #192 review:** the interrupt `toolCallId` reuses the stream's synthetic `{message_id}-{index}` fallback via a shared `_tool_call_id` helper, so a tool call missing an upstream `id` correlates with its `TOOL_CALL_START`.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/events.py` (`Interrupt`, `RunFinishedSuccess`/`Interrupt` outcome union, `RunFinishedEvent.outcome`); `callers/adk.py` (`UpstreamMessage.display_meta`); `agents/adk.py` (collect interrupts → outcome; `_tool_call_id` helper); `clients/adk_caller.py` (`display_meta` populated); `packages/twp-ai/tests/test_adk_agent.py` + `tests/integration/test_chatagent_v3_endpoint.py`; `docs/spec/chatagent_v3.md` §3.4.7, `docs/00_spec.md`, `docs/API.md`. | [x] | Dev |
| T-CAv3S.HITL2 | Red+Green | • **Achieve:** Resume a paused run — `RunAgentInput.resume` (`[{interruptId, status, payload?}]`). `resolved` → upstream `inputData={lastMessageId, message:""}` (payload accepted but not forwarded — upstream is go/no-go only); `cancelled` → no upstream call, `success` outcome; >1 `resolved` → `RUN_ERROR` (`CHATAGENT_INVALID_RESUME`).<br>• **Deliver:** `packages/twp-ai/src/twp_ai/schemas.py` (`ResumeItem` + `RunAgentInput.resume`); `clients/adk_caller.py` (`_resume_input_data`, `ResumeValidationError`); `errors/codes.py` (`CHATAGENT_INVALID_RESUME`); `tests/unit/test_adk_caller.py` + `tests/integration/test_chatagent_v3_endpoint.py`; `docs/spec/chatagent_v3.md`, `docs/00_rule_third_party_api.md` (`lastMessageId` pin), `docs/API.md`. | [x] | Dev |
| T-CAv3S.HITL3 | Red+Green | • **Achieve:** Drop human-in-the-loop interrupt turns from the `GET /chatagent/v3/session` history — a persisted `humanInTheLoopMeta.isInterrupt=true` turn was mapped (via the `node_to_role`/`"assistant"` default) into a stray assistant message; it is a transient approval prompt (surfaced live via `RUN_FINISHED.outcome`, HITL1), not a conversation message, so it must not render in history. Keeps the read consistent with the stream.<br>• **Deliver:** `services/chatagent_session.py` (`_is_interrupt`, filter in `map_session_payload`); `tests/unit/test_chatagent_session_mapper.py` + `tests/integration/test_chatagent_v3_endpoint.py`; `docs/spec/chatagent_v3.md` §3.4.8. | [x] | Dev |

