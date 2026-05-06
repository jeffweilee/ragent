## Discussion: Adopt aiomysql (SQLAlchemy AsyncEngine) for the MariaDB layer

**Date:** 2026-05-06
**Triggered by:** User request; `00_rule.md` §Mandatory Connection Pool endorses `asyncmy`/`asyncpg`-style async pools for async drivers; FastAPI + TaskIQ are async-first.

---

### Master's Opening

Current state: `pymysql` (sync) + SQLAlchemy sync `Engine`. Callers bridge via
`run_in_threadpool` (router) and `anyio.to_thread.run_sync` (worker), consuming
thread-pool slots per DB call. FastAPI routes and TaskIQ tasks are async; they should
`await` DB I/O rather than dispatching to threads. The migration switches to
`aiomysql` + SQLAlchemy `AsyncEngine`, making repos natively async.

Key tension: three categories of sync callers exist — Haystack pipeline components
(sync `run()` methods inside anyio threads), the heartbeat (plain `threading.Thread`),
and `init_schema.py` (startup bootstrap).

---

### Role Perspectives

- 🏗 **Architect**: [Pro] `create_async_engine` + `async with engine.begin()` is idiomatic SQLAlchemy 2.x; eliminates thread overhead for every request-path DB call. [Con] Haystack P1 pipeline is inherently sync; pipeline-component DB calls need a sync→async bridge (resolved by `anyio.from_thread.run()` since they run inside anyio threads).

- ✅ **QA**: [Pro] Repos become `async def` → unit tests use `AsyncMock` + `pytest-asyncio asyncio_mode=auto`; integration tests via testcontainers remain valid. [Con] DSN in `conftest.py` must change to `mysql+aiomysql://`; `init_schema` must keep `mysql+pymysql://` — isolate in `_to_sync_dsn()` helper.

- 🛡 **SRE**: [Pro] No more thread-pool starvation under concurrent ingest; native async probe_mariadb simplifies health probes. [Con] Heartbeat (`threading.Thread`) needs a dedicated per-thread event loop; acceptable since heartbeat fires once per 30 s.

- 🔍 **Reviewer**: [Pro] Router drops all `run_in_threadpool`; reconciler goes fully `async def`; each layer cleaner. [Con] `init_schema.py` must retain `create_engine` + `pymysql` for startup bootstrap — keep `pymysql` as explicit dep alongside `aiomysql`.

- 📋 **PM**: [Pro] MARIADB_DSN stays the same env var; composition root converts `pymysql` → `aiomysql` in DSN string transparently. No operator config change. [Con] New `pytest-asyncio` dev dep; existing unit-test mocks need async upgrade.

- 💻 **Dev**: [Pro] Change per repo method is minimal: add `async` keyword, replace `with engine.begin()` → `async with engine.begin()`, add `await` before `conn.execute`. Service layer follows same pattern. [Con] `anyio.from_thread.run(async_fn, *args)` in Haystack pipeline components is a P1 hack; P2.7 AsyncPipeline will remove the need.

---

### Voting Results

| Role | Vote |
|------|------|
| Architect | Approve |
| QA | Approve |
| SRE | Approve |
| Reviewer | Approve |
| PM | Approve |
| Dev | Approve |

**Result: 6/6 Approve — PASS (no tie-break needed)**

---

### Decision Summary

1. **Repos async**: `DocumentRepository` and `ChunkRepository` — all methods `async def`; `async with self._engine.begin() as conn: await conn.execute(text(...))`.
2. **Engine**: `create_async_engine(aiomysql_dsn)` in composition root. `MARIADB_DSN` keeps `mysql+pymysql://` format; composition root replaces `pymysql` → `aiomysql`.
3. **`init_schema.py`**: keeps `create_engine` + `pymysql` (startup bootstrap is sync by design).
4. **Service**: `IngestService` all methods `async def`.
5. **Router**: drops `run_in_threadpool`; `await svc.method()` directly.
6. **Reconciler**: all methods `async def`; runs under existing `asyncio.run(self._run_async())`.
7. **Worker**: async task `await repo.*` directly; pipeline body runs in anyio thread — pipeline components use `anyio.from_thread.run(async_fn, *args)` bridge.
8. **Heartbeat**: `run_heartbeat` creates one `asyncio.new_event_loop()` per thread; reuses it across ticks.
9. **Health probe**: `probe_mariadb` native `async with engine.connect()`.
10. **Tests**: add `pytest-asyncio>=0.24` dev dep; `asyncio_mode="auto"` in `pyproject.toml`; unit tests use `AsyncMock`.

### Trade-offs Accepted

- Haystack pipeline component bridge (`anyio.from_thread.run`) is a P1 shim; addressed by P2.7 AsyncPipeline.
- `pymysql` stays in the dependency list alongside `aiomysql` (needed for `init_schema`/Alembic).
- Heartbeat creates a new event loop per thread (not using the main pool) — acceptable for 30s-interval single-UPDATE workload.
