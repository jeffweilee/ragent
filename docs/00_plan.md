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

**Counter: 完成 5 / 未完成 1 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv3R.1 | Red+Green | • **Achieve:** `ChatStreamStore` — owner-scoped key; `XADD` append; `XRANGE` `read_after` (Last-Event-ID exclusive); `eos` sentinel + TTL on `mark_done`; `SET NX` single-producer `try_start`; `exists`; Sentinel-aware `from_env`.<br>• **Deliver:** `src/ragent/clients/chat_stream_store.py`; `tests/unit/test_chat_stream_store.py` (fakeredis). | [x] | Dev |
| T-CAv3R.2 | Red+Green | • **Achieve:** New error code `CHATAGENT_STREAM_EXPIRED` (SSE-error only).<br>• **Deliver:** `src/ragent/errors/codes.py`; `docs/spec/error_codes.md`. | [x] | Dev |
| T-CAv3R.3 | Red+Green | • **Achieve:** v3 POST decoupled producer/consumer — background daemon-thread producer tees `ADKAgent.run` into the buffer (single-producer lock); response consumes the buffer, attaching each entry id as the SSE `id:`. No store wired → legacy connection-bound stream. Event sequence unchanged.<br>• **Deliver:** `routers/chatagent_v3.py` (`_spawn_producer`, `_consume_stream`); `tests/unit/test_chatagent_v3_router.py`; `tests/helpers.py` (`parse_sse_events` tolerates `id:`, `parse_sse_ids`). | [x] | Dev |
| T-CAv3R.4 | Red+Green | • **Achieve:** `GET /chatagent/v3/reconnect?thread_id&run_id` — `Last-Event-ID` (exclusive) resume; missing/other-owner buffer → `RUN_ERROR(CHATAGENT_STREAM_EXPIRED)`.<br>• **Deliver:** `routers/chatagent_v3.py` reconnect route + `_stream_expired`; `tests/unit/test_chatagent_v3_router.py` (resume / expired / owner-scoped). | [x] | Dev |
| T-CAv3R.W1 | Behavioral | • **Achieve:** Wire the store into the composition root + v3 registration (built only when `CHATAGENT_API_URL` is set); add stream env vars.<br>• **Deliver:** `bootstrap/composition.py` (`chat_stream_store` field + `from_env`); `bootstrap/app.py` v3 registration; `docs/spec/env_vars.md`. | [x] | Dev |
| T-CAv3R.D1 | Structural | • **Achieve:** Document the resumable-stream contract + reconnect endpoint.<br>• **Deliver:** `docs/spec/chatagent_v3.md` §3.4.7 resumable-stream block; `docs/00_spec.md` pointer if needed. | [x] | Dev |
| T-CAv3R.FE1 | Red+Green | • **Achieve:** mco-clean `@twp/ai` persists `{threadId, runId, lastEventId}` for an in-flight run, reconnects via `GET /chatagent/v3/reconnect` (sends `Last-Event-ID` header) on remount, clears the marker on terminal frame, and falls back to `GET /chatagent/v3/session` on `CHATAGENT_STREAM_EXPIRED`. **(frontend — out of this backend cycle)** | [ ] | Dev |

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

---

## Track T-CAT — Chat Attachments (對話內檔案上傳)

> Source: 2026-06-25 design session. Goal: a user attaches a file to a
> `/chatagent/v3` conversation; the agent references its content on the
> current turn and on every later turn, across all three message
> reconstruction paths (live POST / Redis reconnect / session history).
> Full contract: [`docs/spec/chat_attachments.md`](spec/chat_attachments.md).
>
> **Locked decisions:**
> - No thread-ownership check on attachment reads — identical trust model to
>   existing chat session reads; isolation is `create_user` column + query
>   predicate, not an authz check.
> - Unprotect is **whitelisted by MIME** (`UNPROTECT_MIMES` — PDF/DOCX/PPTX
>   only); plain-text formats skip the external call entirely (no DRM surface
>   to unwrap, avoids wasted API calls).
> - AST (complete + simplified) is encrypted at rest. **Single process-wide
>   DEK**, not per-artifact: `RAGENT_KEK_BASE64` + `RAGENT_ENCRYPTED_DEK_BASE64`
>   injected at startup, `KeyManager` unwraps the DEK once, holds it for the
>   process lifetime. KEK rotation = re-wrap the same DEK offline, update both
>   env vars, restart — no artifact re-encryption needed.
> - `chat_attachment` pipeline (renamed from the earlier "document_structure"
>   working name) only builds AST; it does not encrypt or persist (SRP —
>   those live in `services/chat_attachment_service.py` and
>   `storage/document_store.py` respectively).
> - `DocumentStore` is a Protocol (`put`/`get`/`delete`/`exists`); services
>   depend on the Protocol, never on `MinIODocumentStore` directly (DIP), so a
>   future non-MinIO backend is a single new adapter, zero service changes.
> - Attachment metadata persists inside the existing `<hidden>` preamble as a
>   new `<attachments>` block — no `run_id` indirection, reuses the same
>   binding mechanism `<context>`/`<state>` already use.
> - CSV gets a new `_CsvASTSplitter` (stdlib-only, half-day). XLSX is
>   explicitly descoped this cycle (needs a new dependency).

**Counter: 完成 12 / 未完成 7 / descope 1**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAT.1 | Structural | • **Achieve:** New error codes for the attachment surface. Per R3, every new `error_code` needs a same-commit row in `docs/spec/error_codes.md` (not just the feature spec).<br>• **Deliver:** `src/ragent/errors/codes.py` (`ATTACHMENT_MIME_UNSUPPORTED` 415, `ATTACHMENT_TOO_LARGE` 413, `ATTACHMENT_PARSE_FAILED` 422); `docs/spec/error_codes.md` §API-surface error codes (3 new rows); `docs/spec/chat_attachments.md` §8. | 08b8726 | [x] | Dev |
| T-CAT.2 | Red+Green | • **Achieve:** `AttachmentMime` enum (schema-isolated from `IngestMime`, same six values) + extension fallback for unreliable browser `Content-Type`.<br>• **Deliver:** `src/ragent/schemas/attachments.py`; `tests/unit/test_attachments_schema.py`. | 4489896 | [x] | Dev |
| T-CAT.3 | Red+Green | • **Achieve:** Unprotect whitelist — only PDF/DOCX/PPTX go through `UnprotectClient`; text formats skip the call.<br>• **Deliver:** `UNPROTECT_MIMES` frozenset in `src/ragent/schemas/attachments.py`; `tests/unit/test_attachments_schema.py` (whitelist membership). | 714b4bb | [x] | Dev |
| T-CAT.4 | Red+Green | • **Achieve:** `KeyManager` — unwrap process-wide DEK from `RAGENT_KEK_BASE64` + `RAGENT_ENCRYPTED_DEK_BASE64` once at construction; AES-KW unwrap.<br>• **Deliver:** `src/ragent/security/key_manager.py`; `tests/unit/test_key_manager.py` (wrap/unwrap round-trip, bad-KEK failure). | 84e7582 | [x] | Dev |
| T-CAT.5 | Red+Green | • **Achieve:** `ASTCipher` — AES-256-GCM `encrypt_ast()`/`decrypt_ast()` keyed by `KeyManager.dek`; depends only on `.dek` (ISP).<br>• **Deliver:** `src/ragent/security/ast_cipher.py`; `tests/unit/test_ast_cipher.py` (round-trip, tamper-detection via GCM tag). | 84e7582 | [x] | Dev |
| T-CAT.6 | Red+Green | • **Achieve:** `DocumentStore` Protocol (`put`/`get`/`delete`/`exists`) + `MinIODocumentStore` adapter — injected with the **existing** `MinioSiteRegistry` (same instance ingest already wires; `storage/minio_client.py::MinIOClient` is legacy, unwired code and is **not** reused). `get`/`delete`/`exists` call the registry's existing caller-supplied-key methods (`get_object`/`delete_object`/`stat_object`) directly; `put` calls a new generic `MinioSiteRegistry.put_object(site, object_key, data, length, content_type)` (factors out the S3 call `put_object_default` already makes, parameterized by key instead of `source_app`/`source_id`/`document_id` — `put_object_default` becomes a thin wrapper over it, net dedup).<br>• **Deliver:** `src/ragent/storage/document_store.py`, `src/ragent/storage/minio_document_store.py`; `storage/minio_registry.py` (`put_object` method); `tests/unit/test_minio_document_store.py` (mocked `MinioSiteRegistry`, `autospec=True`); `tests/unit/test_minio_registry.py` (new `put_object` case). | c5f6ab2 | [x] | Dev |
| T-CAT.7 | Structural | • **Achieve:** `chat_attachments` + `chat_attachment_artifacts` tables (no `introduced_run_id` — binding lives in `<hidden>`, not DB).<br>• **Deliver:** `migrations/013_chat_attachments.sql`; alembic registration. | b9c89ef | [x] | Dev |
| T-CAT.8 | Red+Green | • **Achieve:** `attachment_repository.py` — CRUD + `list_by_thread`, `update_status`. CRUD only, no business logic (R3).<br>• **Deliver:** `src/ragent/repositories/attachment_repository.py`; `tests/unit/test_attachment_repository.py`. | b9b816b | [x] | Dev |
| T-CAT.9 | Red+Green | • **Achieve:** `_CsvASTSplitter` — stdlib `csv` module, no new dependency. `text/csv` is new to `IngestMime` (not previously a value); wiring it touches three existing allow-lists, not just the splitter: (1) `schemas/ingest.py` — new `IngestMime.CSV = "text/csv"` member + `MIME_EXTENSIONS[CSV]` entry; (2) `pipelines/ingest/loader.py::ALLOWED_MIMES` — add `IngestMime.CSV`, the actual upload-time gate; (3) `pipelines/ingest/splitter.py` — add `IngestMime.CSV: "csv"` to `_SPLITTER_LABEL`, instantiate `_CsvASTSplitter` in `_MimeAwareSplitter.__init__`, add the matching `elif` branch in `.run()`. `workers/ingest.py` and `routers/admin_ingest.py` need no changes — both already resolve generically via the `IngestMime` enum/`MIME_EXTENSIONS` mapping.<br>• **Deliver:** `src/ragent/schemas/ingest.py` (`IngestMime.CSV`, `MIME_EXTENSIONS` entry); `src/ragent/pipelines/ingest/loader.py` (`ALLOWED_MIMES`); `src/ragent/pipelines/ingest/splitter.py` (`_CsvASTSplitter`, dispatch wiring); `tests/unit/test_splitter_csv.py`. | f368fc6 | [x] | Dev |
| T-CAT.9d | — | • **Descope:** XLSX support — requires a new dependency (e.g. `openpyxl`); out of this cycle. | [~] | — |
| T-CAT.10 | Red+Green | • **Achieve:** `ChatAttachmentPipeline` — load → optional unprotect (gated by T-CAT.3 whitelist) → AST build; "simplified" is derived from "complete" in memory (single parse per attachment, not two), reusing `_MimeAwareSplitter`. No encryption, no persistence (SRP). Scope is the six `AttachmentMime` values only — CSV (T-CAT.9) is an ingest-only addition, not in `AttachmentMime`, so it is out of scope here.<br>• **Deliver:** `src/ragent/pipelines/chat_attachment/pipeline.py`, `src/ragent/pipelines/chat_attachment/ast_builder.py`; `tests/unit/test_chat_attachment_pipeline.py` (mocked unprotect client, all six `AttachmentMime` values). | 2ade99ee | [x] | Dev |
| T-CAT.11 | Red+Green | • **Achieve:** `chat_attachment_service.py` — orchestrates validate → store raw bytes → pipeline.run() → cipher.encrypt_ast() per variant → store artifacts → repository write. Depends only on `DocumentStore`/`ASTCipher` Protocols + `attachment_repository` (DIP).<br>• **Deliver:** `src/ragent/services/chat_attachment_service.py`; `tests/unit/test_chat_attachment_service.py` (autospec mocks for store/cipher/repo/pipeline). | TBD | [x] | Dev |
| T-CAT.12 | Red+Green | • **Achieve:** `POST /chatagent/v3/attachments/upload` + `GET /chatagent/v3/attachments?threadId=` — corrected from an earlier draft's unversioned `/chatagent/attachments`, which would have failed `tests/unit/test_api_versioning.py`'s `^/[a-z][a-z0-9-]*/v[1-9]\d*` contract; nests under the existing `/chatagent/v3` prefix like `/session`/`/reconnect` already do. Router does I/O translation only; no business logic (R3).<br>• **Deliver:** `src/ragent/routers/attachments.py` (separate file, same prefix space as `chatagent_v3.py` — mirrors the existing `admin_ingest.py`/`ingest.py` split under `/ingest/v1`); `tests/integration/test_attachments_router.py` (incl. a case asserting the path matches the version regex). | TBD | [x] | Dev |
| T-CAT.13 | Red+Green | • **Achieve:** `document_artifact_resolver.py` — `attachment_ids` → decrypted ASTs → `<attachments>` block content, for the chat-turn assembly step.<br>• **Deliver:** `src/ragent/services/document_artifact_resolver.py`; `tests/unit/test_document_artifact_resolver.py`. | TBD | [x] | Dev |
| T-CAT.14 | Red+Green | • **Achieve:** `POST /chatagent/v3` accepts `RunAgentInput.attachment_ids`, resolves them via `document_artifact_resolver` into the `<attachments>` block inside `<hidden>` (alongside existing `<context>`/`<state>`), folded into outbound `inputData.message` before the producer thread starts.<br>• **Deliver:** `routers/chatagent_v3.py`; `packages/twp-ai/src/twp_ai/schemas.py` (`Attachment`, `RunAgentInput.attachment_ids`); `tests/integration/test_chatagent_v3_endpoint.py`. | TBD | [x] | Dev |
| T-CAT.15 | — | • **No-op (verified, not implemented):** `ChatStreamStore` only buffers upstream *response* SSE frames (`XADD`/`XRANGE`); it never stashes the request. The `<attachments>` block exists solely in the outbound request built by T-CAT.14, before the buffer exists, and the response stream never echoes `<hidden>` content back (§3.4.7). So `GET /chatagent/v3/reconnect` already replays attachment-bearing runs correctly with zero new code — confirmed by reading `routers/chatagent_v3.py` (no request-side stash exists in this codebase; an earlier draft of this plan incorrectly assumed one). Tracked here only so the verification isn't silently dropped.<br>• **Deliver:** `tests/integration/test_chatagent_v3_endpoint.py` (reconnect-with-attachments case, asserting no extra DB/Redis call is attachment-specific). | — | [x] | Dev |
| T-CAT.16 | Red+Green | • **Achieve:** Session-history read parses `<attachments>` the same way `<context>` is parsed today, then strips it before the rendered text reaches the client.<br>• **Deliver:** `src/ragent/services/chatagent_session.py` (`_extract_attachments_from_hidden`); `tests/unit/test_chatagent_session_mapper.py`. | TBD | [x] | Dev |
| T-CAT.W1 | Behavioral | • **Achieve:** Wire `KeyManager`, `ASTCipher`, `MinIODocumentStore`, `attachment_repository`, `ChatAttachmentPipeline`, `chat_attachment_service`, `document_artifact_resolver` into the composition root; the gated fields were computed but never passed into the returned `Container`, register the attachments router in `app.py`, and thread `document_artifact_resolver`/`attachment_ids` through `chatagent_v3.py`/`ADKCaller` into the `<hidden>` block.<br>• **Deliver:** `bootstrap/composition.py`; `bootstrap/app.py`; `routers/chatagent_v3.py`; `clients/adk_caller.py`; `pipelines/chat_attachment/pipeline.py`; `services/chat_attachment_service.py`; `docs/spec/env_vars.md` (`RAGENT_KEK_BASE64`, `RAGENT_ENCRYPTED_DEK_BASE64`). | TBD | [x] | Dev |
| T-CAT.D1 | Structural | • **Achieve:** Document the full attachment contract.<br>• **Deliver:** `docs/spec/chat_attachments.md` (done — this session); `docs/00_spec.md` §3.4.9 pointer (done); `docs/00_domain_map.md` module entries (done). | [x] | Dev |
| T-CAT.W2 | Behavioral | • **Achieve:** Convert `POST .../upload` from a synchronous in-request pipeline run into the same async worker model `ingest` already uses — fixes API-server request-thread blocking on PDF/DOCX/PPTX unprotect+AST+encrypt. Upload now does fast intake only (store raw → `UPLOADED` row → `dispatcher.enqueue("attachment.process", ...)` → `202`); `workers/attachment.py`'s `attachment.process` task calls the new `ChatAttachmentService.process()`, which atomically claims `UPLOADED→PROCESSING`, runs the pipeline/encrypt/persist steps that used to be inline, and promotes `READY`/`FAILED` (with `error_code`/`error_reason`, never re-raising — no reconciler/attempt-budget in this scope, by explicit user request). New `GET /chatagent/v3/attachments/{attachmentId}` polling endpoint mirrors `GET /ingest/v1/{id}`; list endpoint gets the same `errorCode`/`errorReason` fields for free.<br>• **Deliver:** `migrations/014_chat_attachments_async.sql` (+ folded into `schema.sql`); `src/ragent/repositories/attachment_repository.py` (`claim_for_processing`, `update_status` error kwargs); `src/ragent/services/chat_attachment_service.py` (`upload`/`process` split); `src/ragent/workers/attachment.py` (new); `src/ragent/worker.py`, `src/ragent/bootstrap/app.py`, `src/ragent/bootstrap/composition.py` (wiring); `src/ragent/routers/attachments.py` (202, new GET, error fields); `src/ragent/errors/codes.py` (`ATTACHMENT_NOT_FOUND`); `docs/spec/error_codes.md`, `docs/spec/chat_attachments.md` §7/§9/§10, `docs/00_spec.md` §3.4.9; `tests/unit/test_attachment_repository.py`, `tests/unit/test_chat_attachment_service.py`, `tests/unit/test_workers_attachment.py`, `tests/integration/test_attachments_router.py`. | TBD | [x] | Dev |
