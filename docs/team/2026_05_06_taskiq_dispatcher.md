## Discussion: TaskIQ Producer-Side Bug — `broker.enqueue()` does not exist

### Master's Opening

Triggered by a runtime defect on the API server: `IngestService.create` calls
`self._broker.enqueue("ingest.pipeline", document_id=...)` but production
wiring (`bootstrap/app.py`, `reconciler.py`) injects the raw
`taskiq_redis.ListQueueBroker`, which has no `enqueue` method. Tests pass
because mocks accept any attribute. Three latent issues:

1. **Wrong producer API** — TaskiqBroker is enqueued via
   `await registered_task.kiq(**kwargs)` (per TaskIQ docs). `.enqueue()`
   was an internal contract never implemented.
2. **Producer-side broker never started** — TaskIQ requires
   `await broker.startup()` before producing; only the worker entrypoint
   starts the broker today. API + Reconciler will fail at first kiq.
3. **Unregistered task** — `reconciler._repair_multi_ready` enqueues
   `"ingest.supersede"` but no `@broker.task("ingest.supersede")` exists in
   `workers/ingest.py` (T3.2d delivery never registered the task — only the
   service method exists).

Relevant rules: B25 (kiq dispatch), B27 (broker topology), R1/R3
(reconciler recovery), C9 (no behavior change visible to clients), spec
§3.1, §5 (Reconciler), §3.2 (worker).

### Role Perspectives

- 🏗 **Architect**:
  - **Pro (introduce `Dispatcher` abstraction):** isolates TaskIQ from the
    service/reconciler layer; respects DDD bounded context (the service
    cares about "dispatch this work", not the broker brand). Aligns with
    spec §3 (kiq is an implementation detail).
  - **Con:** any abstraction risks divergence from upstream TaskIQ
    semantics (timeouts, labels, schedule_by_time). Mitigated by keeping
    the dispatcher *thin* — enqueue-only, no scheduling.

- ✅ **QA**:
  - **Pro:** existing unit tests (`test_ingest_service_create.py`,
    `test_reconciler_redispatch.py`, `test_reconciler_multi_ready_repair.py`)
    already exercise a `dispatcher.enqueue(label, **kwargs)` shape via
    mocks — preserve this surface. Reconciler tests need
    `MagicMock → AsyncMock` because the call site must `await` (real kiq
    is async); change is mechanical and explicit.
  - **Con:** mocked tests have hidden the production defect for months;
    add at least one **integration** test against an in-memory TaskIQ
    broker to catch this contract drift.

- 🛡 **SRE**:
  - **Pro:** lifespan-managed `broker.startup()/shutdown()` is mandatory
    for graceful API restarts and accurate `/readyz` (B27). Without it,
    first ingest after deploy will 500.
  - **Con:** failure during `broker.startup()` must block app readiness,
    not just log; otherwise we ship a broken API that 202s but never
    enqueues. Lifespan must propagate the exception.

- 🔍 **Reviewer**:
  - **Pro:** TaskIQ idiomatic call is `await task.kiq(**kwargs)` on the
    decorated task object (typed, label-checked at import time). Looking
    up by string (`broker.find_task("ingest.pipeline")`) loses the type
    contract and only fails at runtime.
  - **Con:** call sites are sync (IngestService.create) or in already-async
    flows (Reconciler). Mixing the two requires either two dispatcher
    interfaces or a sync→async bridge. Choose one and document it.

- 📋 **PM**:
  - **Pro:** the fix is **C9-conformant** (no client-visible behavior
    change) and unblocks T2.8 + T5.1 + T5.7 + T5.9 in production. Ship now.
  - **Con:** scope creep risk. Limit this round to the
    dispatcher + lifespan + supersede-task; do **not** rewrite the
    `_has_fan_out` polymorphism in `IngestService.delete` (separate
    track).

- 💻 **Dev**:
  - **Pro:** introduce `bootstrap/dispatcher.py` exposing two narrow
    classes:
    - `TaskiqDispatcher` — async; `await dispatcher.enqueue(label, **kwargs)`
      → resolves task via the `@broker.task` registry, awaits `task.kiq`.
      Used by Reconciler.
    - `BlockingTaskiqDispatcher` — wraps the async one with sync
      `enqueue(label, **kwargs)` that bridges via
      `anyio.from_thread.run` (works because IngestService.create runs
      under FastAPI's `run_in_threadpool` worker thread, with an
      asyncio loop on the main thread). Used by IngestService.
  - **Pro:** task lookup happens once at construction (cached map) — no
    per-call `find_task` overhead.
  - **Con:** anyio.from_thread requires a running loop on the parent
    thread; this is true under FastAPI but **not** in a bare
    `pytest` invocation. Unit tests must keep injecting mocks with
    `.enqueue()` (already the case — no test churn).

### Conflict Identification

1. Single dispatcher type (async-only, push `await` everywhere) vs. two
   dispatcher types (sync for service, async for reconciler).
2. Task lookup by string label vs. injecting the decorated task object.

### Voting Results

| Role | Vote | Note |
|---|---|---|
| PM | ✅ Approve | small, surgical, unblocks production |
| Architect | ✅ Approve | bounded-context boundary preserved |
| QA | ✅ Approve | preserves unit-test shape; adds integration test |
| SRE | ✅ Approve | with lifespan exception propagation |
| Reviewer | ✅ Approve | accept string-label lookup as pragmatic seam |
| Dev | ✅ Approve | clear ownership of sync↔async bridge |

**Result: Pass (6/6 Approve)**

### Decision Summary

**Approved**:
1. Add `bootstrap/dispatcher.py` with two classes:
   `TaskiqDispatcher` (async) and `BlockingTaskiqDispatcher` (sync,
   wraps async via `anyio.from_thread.run`).
2. FastAPI lifespan: `await broker.startup()` on enter, `await
   broker.shutdown()` on exit. Startup failure aborts app boot (no
   silent degradation).
3. Reconciler: redispatch + multi-ready arms become `async`; build/teardown
   the broker inside `_run_async` via `await broker.startup()/shutdown()`.
4. Register `@broker.task("ingest.supersede")` in
   `src/ragent/workers/ingest.py` so the supersede label resolves at
   producer-side dispatch.
5. `IngestService` keeps `broker` parameter name for backward test
   compat — production wiring passes the `BlockingTaskiqDispatcher`
   instead of the raw broker.
6. Reconciler tests (`test_reconciler_redispatch.py`,
   `test_reconciler_multi_ready_repair.py`) switch broker mock from
   `MagicMock` → `AsyncMock`; assertions become
   `enqueue.assert_awaited_once_with(...)`.
7. Add `tests/unit/test_dispatcher.py` covering the dispatcher contract
   (label lookup, kwargs passthrough, missing-task error).

**Accepted trade-offs**:
- String-label lookup (vs. injecting decorated tasks) — keeps the
  `dispatcher.enqueue(label, **kwargs)` surface stable. Catch missing
  tasks at call time with explicit `RuntimeError`.
- `IngestService.create` stays sync — bridges via anyio. Unit-test mocks
  remain sync `MagicMock`, no asyncio plumbing in test setup.

### Pending Items
- `IngestService` still conflates "task dispatcher" and "plugin
  registry" through `_has_fan_out`. Tracked as separate refactor (out
  of scope for this round).
- An end-to-end integration test against
  `taskiq.brokers.inmemory_broker.InMemoryBroker` to lock the producer
  contract — added as a follow-up task in `docs/00_plan.md`.
