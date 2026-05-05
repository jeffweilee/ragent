# Discussion: Structlog — ISO8601 / API trace / business / error logs + OTEL coverage

> Source: user directive 2026-05-05
> Date: 2026-05-05
> Mode: RAGENT Agent Team (6 voting members + Master)
> Predecessors: `2026_05_04_phase1_round4_supersede_uid_review.md`

---

## Master's Opening

**Triggered context:**
- User asks for `structlog` integration with four log categories: ISO8601 timestamps, API trace logs, business meaning logs, error logs.
- Follow-up directives: (1) integrate with OTEL; (2) ensure chat & retrieve flows expose observability; (3) confirm no conflict with Haystack 2.x OTEL wiring; (4) console output; (5) `LOG_FORMAT=console|json` switch; (6) add a privacy rule to `docs/00_rule.md` — business + API trace logs carry identity, never sensitive content.
- Existing state: stdlib `logging` with ad-hoc `event=...` strings; `setup_tracing()` (`src/ragent/bootstrap/telemetry.py:31-51`) already wires Haystack 2.x → `OpenTelemetryTracer`; FastAPI auto-instrumented; chat/retrieve routers + LLM/Embedding/Rerank clients have **no** spans.
- Plan file approved: `/root/.claude/plans/add-structlog-iso8601-datetime-expressive-truffle.md`.

**Topics this round:**
- **L1** Structlog config + ISO8601 + service binding + JSON-to-stdout + `LOG_FORMAT=console|json`.
- **L2** `RequestLoggingMiddleware` (`api.request` / `api.error`, `request_id`, header echo, `/livez` `/readyz` `/metrics` skipped).
- **L3** OTEL span coverage for chat/retrieve routers and LLM/Embedding/Rerank clients; nesting under existing FastAPI + Haystack spans.
- **L4** Privacy rule in `docs/00_rule.md` (identity-yes / content-no) + denylist redaction processor.
- **L5** Migration of existing `logging.getLogger` call sites; back-compat for 3rd-party stdlib loggers via `ProcessorFormatter`.

---

## Round 1 — Role Perspectives

### L1 · Structlog configuration

- 🏗 **Architect**: [Pro] Single `configure_logging(service)` keeps the entry-point contract narrow; `merge_contextvars` + `TimeStamper(fmt="iso", utc=True)` is the canonical structlog 24.x recipe. [Con] Two renderers (`ConsoleRenderer` for dev, `JSONRenderer` for prod) introduces a code path divergence. **Position**: gate by `LOG_FORMAT` env (default `json`); both renderers reuse the same upstream processor chain so only the terminal renderer differs.
- ✅ **QA**: [Pro] Deterministic test via `structlog.testing.capture_logs`; ISO8601 regex assertion is straightforward. [Con] `ConsoleRenderer` output is not asserted in tests. **Position**: assert JSON path in unit tests; smoke `LOG_FORMAT=console` only in manual verification.
- 🛡 **SRE**: [Pro] JSON to stdout works in Docker/k8s with no shipper changes. [Con] uvicorn's default access log will double-log. **Position**: disable `uvicorn.access` logger in `configure_logging` to keep the `api.request` line authoritative.
- 🔍 **Reviewer**: [Pro] `EventRenamer("message")` keeps the JSON Loki/ELK-friendly. [Con] `event=` keyword convention in existing call sites becomes the first positional arg — verify no regex-based log scrapers depend on `event=`. **Position**: spec only — no external scrapers; safe to rename.
- 📋 **PM**: [Pro] One env var (`LOG_FORMAT`) is a low-cost ergonomic win for local dev. **Position**: ship.
- 💻 **Dev**: [Pro] `structlog.contextvars.bind_contextvars(service=...)` once at startup makes every line carry the service name with zero per-call cost. **Position**: confirm.

### L2 · RequestLoggingMiddleware

- 🏗 **Architect**: [Pro] Starlette `BaseHTTPMiddleware` is the right seam; placing it **before** `_x_user_id_middleware` ensures even the 422 missing-X-User-Id response is logged. [Con] `BaseHTTPMiddleware` adds an extra `await` hop; for high-RPS this is ~30µs. **Position**: acceptable; revisit only if perf budget is breached.
- ✅ **QA**: [Pro] Test matrix: success path, exception path, custom `X-Request-Id`, skipped `/livez`, header echo. **Position**: test all five.
- 🛡 **SRE**: [Pro] Re-raising after logging keeps the existing `@app.exception_handler(Exception)` path intact, so RFC 9457 problem responses are unchanged. [Con] Must `clear_contextvars()` in `finally` to prevent context leakage between requests. **Position**: enforce `try/finally`.
- 🔍 **Reviewer**: [Pro] Honoring incoming `X-Request-Id` enables client-side correlation without breaking existing clients. [Con] Validate length (≤128) to avoid log injection. **Position**: trim/validate.
- 📋 **PM**: [Pro] Per-request logs unblock debugging without OTEL infra. **Position**: ship.
- 💻 **Dev**: [Pro] `structlog.contextvars.bind_contextvars(request_id=..., user_id=...)` makes downstream router/client logs auto-include the IDs with no plumbing. **Position**: confirm.

### L3 · OTEL span coverage (chat, retrieve, clients) — Haystack 2.x

- 🏗 **Architect**: [Pro] Verified: `haystack.tracing.tracer.actual_tracer = OpenTelemetryTracer(...)` (`telemetry.py:51`) uses the **same** global `TracerProvider`; new router/client spans created with `tracer.start_as_current_span(...)` become parents of every `@component` span via OTEL contextvars. [**No conflict.**] [Con] If a future contributor instantiates a second `TracerProvider`, double export occurs. **Position**: keep `setup_tracing` as the single source; add a comment.
- ✅ **QA**: [Pro] `InMemorySpanExporter` + isolated `TracerProvider` in tests gives deterministic span trees: `chat.request → chat.retrieval → chat.llm`. [Con] Haystack component spans are integration-only (need real ES) — assert nesting in integration test, not unit. **Position**: split.
- 🛡 **SRE**: [Pro] When `OTEL_EXPORTER_OTLP_ENDPOINT` unset, `start_as_current_span` is a no-op; zero overhead. [Pro] Pinning `is_content_tracing_enabled = False` prevents prompt/answer leakage on Haystack upgrades. **Position**: pin explicitly + gate by `HAYSTACK_CONTENT_TRACING_ENABLED` (default off).
- 🔍 **Reviewer**: [Pro] Span attributes are an allow-list (counts, durations, status codes) — aligns with the new privacy rule. [Con] LLM stream span lifetime must wrap the full async iterator, not just the request. **Position**: use `start_as_current_span` as a context manager around the `async for` loop.
- 📋 **PM**: [Pro] Closes the observability gap for the most expensive code paths (LLM/embedding/rerank). **Position**: ship.
- 💻 **Dev**: [Pro] Three clients × ~10 LoC each. [Pro] Reuses `from opentelemetry import trace; tracer = trace.get_tracer(__name__)`. **Position**: confirm.

### L4 · Privacy rule (identity-yes / content-no)

- 🏗 **Architect**: [Pro] Allow-listed attributes (`user_id`, `request_id`, `trace_id`, `document_id`, counts, sizes, durations, status codes) cover audit needs without leaking content. [Con] Allow-list-only is hard to enforce mechanically. **Position**: ship a **denylist processor** as the safety net (`query`, `prompt`, `messages`, `completion`, `chunks`, `embedding`, `documents`, `body`, `authorization`, `cookie`, `password`, `token`, `secret`).
- ✅ **QA**: [Pro] Three tests: denylisted key dropped, exception message truncated, middleware never logs query string. **Position**: add `tests/unit/bootstrap/test_logging_redaction.py`.
- 🛡 **SRE**: [Pro] Sensitive content out of logs simplifies retention compliance (no PII window). [Con] Need to preserve enough for debugging. **Position**: log lengths, hashes, counts; never raw values.
- 🔍 **Reviewer**: [Pro] Codifying in `docs/00_rule.md` means future PRs must comply. **Position**: add the **Logging Rule** subsection.
- 📋 **PM**: [Pro] Aligns with security/compliance posture. **Position**: ship.
- 💻 **Dev**: [Pro] Denylist processor is ~15 LoC; runs early in the chain so even contextvars can't smuggle content. **Position**: confirm.

### L5 · Migration

- 🏗 **Architect**: [Pro] Behavioral substitution (`logging.getLogger` → `structlog.get_logger`) plus kwargs conversion is a pure log-format change; no business behavior moves. [Con] Per CLAUDE.md "Tidy First", structural and behavioral changes must not mix. **Position**: this PR is **behavioral** (new feature: structured logging); commit prefix `[BEHAVIORAL]`.
- ✅ **QA**: [Pro] Existing tests don't assert log output, so migration is invisible to them. [Con] Must run full suite to confirm no regression. **Position**: `uv run pytest -q` gate before commit.
- 🛡 **SRE**: [Pro] `ProcessorFormatter` routes uvicorn/sqlalchemy/taskiq stdlib logs through the same JSON chain — unified pipeline. **Position**: confirm.
- 🔍 **Reviewer**: [Pro] Surgical-changes rule satisfied: only loggers and span wrappers touched. **Position**: confirm.
- 📋 **PM**: [Pro] Single PR; clear scope. **Position**: confirm.
- 💻 **Dev**: [Pro] Conversion is mechanical; ~5 files. **Position**: confirm.

---

## Conflict Identification

No live conflicts. Master notes two enforced trade-offs:
1. **Allow-list vs. denylist** for privacy → ship denylist processor as safety net + document the allow-list as policy in `docs/00_rule.md`.
2. **`BaseHTTPMiddleware` overhead** → accepted; revisit on a perf budget breach.

---

## Voting Results

| Topic | 🏗 | ✅ | 🛡 | 🔍 | 📋 | 💻 | Result |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| L1 Structlog config + LOG_FORMAT | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| L2 RequestLoggingMiddleware | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| L3 OTEL spans (chat/retrieve/clients) + Haystack 2.x | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| L4 Privacy rule + denylist processor | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| L5 Logger migration | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

**All five topics pass 6/6.** Plan approved for implementation.

---

## Decision Summary

**Approved**:
- `structlog>=24.1.0` dependency.
- `src/ragent/bootstrap/logging_config.py` with processor chain (contextvars merge → level → ISO8601 timestamp → OTEL trace injector → stack/exc info → `EventRenamer("message")` → renderer).
- `LOG_FORMAT=console|json` env var (default `json`).
- `LOG_LEVEL` env var honored (existing).
- `HAYSTACK_CONTENT_TRACING_ENABLED` env var (default off); pin `haystack.tracing.tracer.is_content_tracing_enabled = False` in `setup_tracing`.
- `src/ragent/middleware/logging.py` (`RequestLoggingMiddleware`).
- Router-level OTEL spans (`chat.request`/`chat.retrieval`/`chat.build_messages`/`chat.llm`; `retrieve.request`/`retrieve.pipeline`/`retrieve.dedupe`) — emit business log on completion.
- Client-level OTEL spans (`llm.chat`/`llm.stream`/`embedding.embed`/`rerank.score`) + success/error logs.
- Logger migration in `bootstrap/app.py`, `bootstrap/init_schema.py`, `reconciler.py`, `workers/ingest.py`.
- **Logging Rule** in `docs/00_rule.md` (identity-yes / content-no) + denylist redaction processor.
- Tests: `tests/unit/bootstrap/test_logging_config.py`, `test_logging_redaction.py`, `tests/unit/middleware/test_logging_middleware.py`, `tests/unit/routers/test_chat_tracing.py`, `test_retrieve_tracing.py`, `tests/unit/clients/test_clients_tracing.py`, `tests/integration/test_logging_pipeline.py`.

**Accepted Trade-offs**:
- `BaseHTTPMiddleware` per-request overhead (~30µs); revisit on perf breach.
- Privacy enforcement is **denylist + policy** (not allow-list-only); revisit if a leak is found in journal.
- Orphan log fields named in 3rd-party libs not yet migrated stay as plain JSON via `ProcessorFormatter`.

**Out of Scope (deferred)**:
- Log shipping config (Loki/Vector).
- Sampling / rate limiting on log volume.
- PII detection beyond denylist.

---

## Pending Items

None. Single round, clean pass.

---

## Next

Enter TDD implementation per the approved plan file:
1. Branch already on `claude/add-structlog-logging-Xbc6L`.
2. Red → Green → Refactor per CLAUDE.md TDD WORKFLOW. Every step verified by `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`.
3. Commit prefix `[BEHAVIORAL]` for new feature commits, `[STRUCTURAL]` for the logger-rename commit.
4. Update `docs/00_rule.md` (Logging Rule), `docs/00_spec.md` (Structured Logging subsection), `docs/00_plan.md` (Phase 2 Stability row, mark `[x]` after green).
5. Push to `claude/add-structlog-logging-Xbc6L`. **No PR** unless user asks.
