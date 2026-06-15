# 00_plan.md — Master TDD Implementation Checklist

> Source: `docs/00_spec.md` · Workflow: `CLAUDE.md §THE TDD WORKFLOW`
> Each `[ ]` = one Red→Green→Refactor cycle; each cycle = one (or two) commits.
> Completed and descoped tracks are archived in [`docs/00_plan_done.md`](00_plan_done.md).

## Status legend
- `[x]` delivered
- `[ ]` TODO
- `[~]` descoped / deferred (not doing in this cycle)

---

## Track T-SR — Supersede Race: older-winner demote guard (issue #179)

> MVCC asymmetry in `_promote_or_demote`: the election subquery uses an MVCC
> snapshot while the sibling-demote UPDATE uses a current read. An older winner
> can permanently demote a strictly newer sibling if that sibling's claim committed
> between the two statements. Fix: constrain the demote to siblings that are
> strictly older by `(created_at, document_id)` — the same tie-break as the election.

**Counter: 完成 2 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-SR.1 | Red | • **Achieve:** Expose the bug — verify current demote SQL will demote a newer sibling when an older doc wins via MVCC anomaly.<br>• **Deliver:** `tests/integration/test_worker_atomic_promote.py::test_winner_never_demotes_strictly_newer_sibling` — seeds OLDEST/WINNER/NEWER, forces WINNER to READY (simulating MVCC win), runs sibling-demote directly, asserts OLDER is DELETING and NEWER is still PENDING. Must **fail** against current production code.<br>• **Success criteria:** Test collected by pytest; OLDEST assertion = DELETING, NEWER assertion = PENDING both pass with the fixed SQL. | [x] | QA |
| T-SR.2 | Green | • **Achieve:** Patch `_promote_or_demote` so the sibling-demote UPDATE only touches rows with `(created_at, document_id) < (winner.created_at, winner.document_id)`.<br>• **Deliver:** Fixed SQL in `src/ragent/repositories/document_repository.py::_promote_or_demote`; updated B41 note in `docs/00_spec.md`; T-SR.1 test now passes.<br>• **Success criteria:** `make test-gate` green; B41 in `docs/00_spec.md` references the demote guard; the demote UPDATE WHERE clause contains the `(created_at, document_id)` ordering guard. | [x] | Dev |

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

## Track T-CAUI — ChatAgent v3 client-side UI tools (AGENTIC_UI_TOOL dispatcher)

> Source: 2026-06-15 design session. `/twp/v1/run` (native DirectLLMAgent) already
> supports client-provided tools because ragent owns the LLM loop and passes
> `tools` through with `tool_choice=auto`. `/chatagent/v3` proxies an upstream
> ChatAgent that **only invokes tools pre-registered in its own registry** — a
> live test confirmed message-embedded tool definitions return `tool_not_found`.
>
> **Locked decisions:**
> - A single generic tool, **`AGENTIC_UI_TOOL`**, is pre-registered upstream as a
>   client-side (emit-and-suspend) dispatcher. The upstream calls it with the chosen
>   frontend tool wrapped inside (`{tool_name, arguments}`); ragent **unwraps** the
>   envelope so the frontend sees a normal tool call for the real tool — it never
>   sees `AGENTIC_UI_TOOL`. Same `toolCallId` is carried through unwrap.
> - The per-request frontend tool catalog rides in the existing `<hidden>`
>   machine-context block as a `<tools>` section (frontend strips it; session
>   history strips it; the upstream system prompt already reads `<hidden>`).
> - Turn 1 = emit tool-call events then `RUN_FINISHED` (parity with `/twp/v1/run`);
>   whether a follow-up run happens is the frontend's decision (continuation run on
>   the same `threadId`, upstream resumes via session memory).
> - **Upstream prerequisites (external, P0):** `AGENTIC_UI_TOOL` registered
>   client-side (not synchronously server-executed); the resume wire format for the
>   continuation turn (`_compose_message` tool-result branch is isolated pending it).

**Counter: 完成 6 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAUI.1 | Red+Green | • **Achieve:** Define the `AGENTIC_UI_TOOL` contract (name + envelope unwrap) as a single source of truth in twp-ai.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/client_tools.py::AGENTIC_UI_TOOL_NAME` + `unwrap_agentic_ui_call`; `packages/twp-ai/tests/test_client_tools.py` (happy path + malformed envelope → ValueError).<br>• **Success criteria:** unwrap returns `(inner_tool_name, inner_arguments_json)`; missing/non-string `tool_name` and non-JSON args raise `ValueError`. | [x] | Dev |
| T-CAUI.2 | Red+Green | • **Achieve:** Unwrap `AGENTIC_UI_TOOL` calls in the ADK relay so the frontend receives the inner tool's `TOOL_CALL_*` events (same `toolCallId`); non-dispatcher tool calls unchanged; malformed envelope → `RUN_ERROR`.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/agents/adk.py::_relay`; tests in `packages/twp-ai/tests/test_adk_agent.py`.<br>• **Success criteria:** an upstream `AGENTIC_UI_TOOL` call emits `TOOL_CALL_START.toolCallName == inner name` and `TOOL_CALL_ARGS.delta == inner args JSON`; a `search` tool call is byte-for-byte unchanged. | [x] | Dev |
| T-CAUI.3 | Red+Green | • **Achieve:** Inject the frontend tool catalog into the upstream message as a `<tools>` section inside `<hidden>`; neutralize literal wrapper tags; empty `tools` keeps the message unchanged.<br>• **Deliver:** `src/ragent/clients/adk_caller.py` (`_context_preamble` + `_WRAPPER_TAG_RE`); `tests/unit/test_adk_caller.py` (catalog present/absent, neutralization); update the existing `tools-not-folded` assertion.<br>• **Success criteria:** non-empty `tools` → `<tools>[…]</tools>` inside `<hidden>`; empty `tools` → message identical to today; a `</tools>` in a tool field is escaped. | [x] | Dev |
| T-CAUI.4 | Red+Green | • **Achieve:** Define the `AGENTIC_UI_TOOL` MCP descriptor and advertise it in `tools/list`; a `tools/call` for it returns an `isError` result (client-side, not server-executable) instead of running retrieval.<br>• **Deliver:** `src/ragent/routers/mcp_tools/agentic_ui.py`; `routers/mcp.py` registration + tools/call branch; `tests/unit/test_mcp_router.py` (or existing MCP test).<br>• **Success criteria:** `tools/list` includes `AGENTIC_UI_TOOL` with `{tool_name, arguments}` schema; `tools/call AGENTIC_UI_TOOL` → `isError: true`, retrieval pipeline not invoked. | [x] | Dev |
| T-CAUI.5 | Red+Green | • **Achieve:** Continuation/resume — when the latest turn is a frontend tool result (`role="tool"`), forward the result to the upstream instead of re-sending the old user question; normal user turns unchanged.<br>• **Deliver:** `src/ragent/clients/adk_caller.py::_compose_message` tool-result branch (isolated `_format_tool_result`, pending upstream resume format P0-0-3); `tests/unit/test_adk_caller.py`.<br>• **Success criteria:** a `[user, assistant(toolCalls), tool(result)]` continuation forwards the tool result + `toolCallId` and does NOT echo the old user text; a plain user turn is unchanged. | [x] | Dev |
| T-CAUI.D1 | Structural | • **Achieve:** Document the client-side tool flow.<br>• **Deliver:** `docs/00_spec.md` §3.4.7 (replace “client tool-call continuation not yet implemented” with the AGENTIC_UI_TOOL contract); `docs/spec/twp_ai.md`; `docs/spec/mcp_server.md` (new tool).<br>• **Success criteria:** spec describes catalog injection, unwrap, terminate, and the continuation/resume contract; the “not yet implemented” line is gone. | [x] | Dev |

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

**Counter: 完成 11 / 未完成 1 / descope 0**

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
