## 6. Standards

- **Layers:** Router (HTTP only) → Service (orchestration) → Repository (CRUD only).
- **Methods:** ≤ 30 LOC, max 2-level nesting. Utilities in `utility/`.
- **IDs:** UUIDv7 + Crockford Base32 (26 chars). **DateTime:** end-to-end UTC + `Z` suffix.
- **DB:** no physical FK; index every `WHERE / JOIN / ORDER BY` field.
- **Quality gate:** `uv run ruff format . && uv run ruff check . --fix && uv run pytest --cov=src/ragent --cov-branch --cov-fail-under=92` before every commit. **Test coverage floor: 92% (line + branch)** — CI rejects drops; DoD requirement.
- **TDD commits:** `[STRUCTURAL]` or `[BEHAVIORAL]` prefix; never mixed.
- **JSON naming convention (B21):** within request/response bodies, **identifier and resource fields are `snake_case`** (`document_id`, `source_id`, `source_app`, `source_title`, `error_code`, `next_cursor`, `task_id`, `trace_id`); **LLM token/config knobs are `camelCase`** (`maxTokens`, `promptTokens`, `completionTokens`, `totalTokens`, `temperature`, `topP` if added later) — preserved to match upstream OpenAI-shape expectations. Within a single body both styles may coexist; the rule above resolves which to use for any new field.

### 6.1 Schema & Migration (B3)

Two artefacts, **both versioned in git**, both consulted at boot:

| Artefact | Path | Purpose | Owner |
|---|---|---|---|
| **Consolidated snapshot** | `migrations/schema.sql` | Single-file DDL representing the current target schema. Updated **in lockstep with every incremental migration**. Used by fresh dev/CI/testcontainers bring-up (`mariadb < schema.sql` → instant ready). | Dev |
| **Incremental migrations** | `alembic/sql/upgrade/NNN_<slug>.sql` + `alembic/sql/downgrade/NNN_<slug>.sql` (e.g. `001_initial.sql`, `002_ingest_v2.sql`) | Forward/reverse ALTER scripts replayed in order by a hand-rolled `MIGRATION_CHAIN` (`alembic/env.py`), guarded by `verify_and_get_chain()` (refuses to run on a numbering gap or missing file). `alembic upgrade head` / `downgrade base`/`-N`. Production / staging path **(B64, supersedes the `migrations/NNN_*.sql` location)**. | Dev |

**Boot-time auto-init (idempotent):**
- On startup, the bootstrap module runs `CREATE TABLE IF NOT EXISTS … / CREATE INDEX IF NOT EXISTS …` against MariaDB derived from `migrations/schema.sql`, and `PUT /<index>` for ES if the index does not exist — **using the JSON body in `resources/es/<index>.json`** (e.g. `resources/es/chunks_v1.json`, B26). Existing tables/indexes are left untouched.
- Resource files are the single source of truth for ES index definitions; spec §5.2 mirrors them in prose. `tests/integration/test_es_resource_drift.py` parses both and rejects drift.
- Auto-init is for dev/test bring-up convenience only; production migrations MUST go through Alembic (DB) or a controlled `PUT /<index>-vN` + reindex flow (ES). Boot-init refuses to run any `ALTER` or ES mapping update — schema drift is logged as `event=schema.drift` and surfaces in `/readyz` as a degraded state, not an automatic mutation.

**Invariant:** `schema.sql` ≡ replaying `001 → NNN`. CI enforces this with `tests/integration/test_schema_drift.py` (apply both paths to two scratch DBs, `mysqldump` both, diff must be empty).

### 6.2 Module Layout

> Canonical project tree. Every file is produced by exactly one Green/Structural plan row; no file is written outside this layout. Layered dependency rule: **routers → services → repositories**; **plugins / clients / storage / pipelines** are leaf concerns injected via the composition root (B30). **Only `bootstrap/composition.py` reads env vars** — every other module receives its config via constructor argument (B17, B30).

```
ragent/
├── pyproject.toml
├── .env.example                                  # T0.11 (B30) — operator-facing config artifact
├── Dockerfile.es-test                            # T0.9  — ES container with analysis-icu pre-installed
├── deploy/k8s/reconciler-cronjob.yaml            # T5.2  (B9) — Reconciler CronJob manifest
├── migrations/
│   ├── schema.sql                                # T0.8a (B3) — consolidated snapshot
│   └── 001_initial.sql                           # T0.8  (B3) — forward-only Alembic
├── resources/es/chunks_v1.json                   # T0.8e (B26) — ES index source of truth
├── src/ragent/
│   ├── api.py                                    # T7.5d — `python -m ragent.api`     (uvicorn launcher)
│   ├── worker.py                                 # T7.5e — `python -m ragent.worker`  (TaskIQ launcher)
│   ├── reconciler.py                             # T5.2  — `python -m ragent.reconciler` (one-shot, B9)
│   ├── bootstrap/
│   │   ├── guard.py                              # T7.5  — RAGENT_ENV/AUTH/HOST/LOG_LEVEL guard
│   │   ├── broker.py                             # T0.10 (B27/B30) — TaskIQ broker; sole `@broker.task` import
│   │   ├── composition.py                        # T7.5a (B30) — composition root / DI Container; sole env-reader
│   │   ├── init_schema.py                        # T0.8d (B3, B26) — CREATE IF NOT EXISTS / PUT index
│   │   └── app.py                                # T7.5c — FastAPI `create_app()` + lifespan auto-init
│   ├── routers/
│   │   ├── ingest.py                             # T2.14 (B5) — /ingest CRUD + RFC 9457
│   │   ├── chat.py                               # T3.10/T3.12 (B12, B6) — /chat + /chat/stream
│   │   ├── mcp.py                                # T-MCP.* — /mcp/v1 JSON-RPC 2.0 server (§3.8)
│   │   └── health.py                             # T7.8  (B4, C9) — /livez /readyz /metrics
│   ├── services/ingest_service.py                # T2.8 / T2.10 / T2.12 / T3.2d — create / delete / list / supersede
│   ├── repositories/
│   │   ├── document_repository.py                # T2.2  (B11/B14/B16/B25/B29) — CRUD + heartbeat + supersede helpers
│   │   └── chunk_repository.py                   # T2.4
│   ├── plugins/
│   │   ├── protocol.py                           # T1.2  — `ExtractorPlugin` Protocol (frozen, §3.3)
│   │   ├── registry.py                           # T1.7  — `PluginRegistry`, fan_out, per-plugin timeout
│   │   ├── vector.py                             # T1.10 / T1.12 (B15/B17/B29) — VectorExtractor (DI)
│   │   └── stub_graph.py                         # T1.4  — no-op P1 placeholder for §4.4 graph row
│   ├── pipelines/
│   │   ├── factory.py                            # T3.2 / T3.5a — ingest + chat factories (CHAT_JOIN_MODE dispatch)
│   │   ├── ingest.py                             # T3.2  (B1) — Haystack components + AST splitters
│   │   └── chat.py                               # T3.6  (B23) — `build_retrieval_pipeline` + SourceHydrator
│   ├── clients/
│   │   ├── auth.py                               # T4.2  (P-F, S9) — TokenManager (J1→J2, single-flight)
│   │   ├── embedding.py                          # T4.4  (C8) — bge-m3, batched, asymmetric timeouts
│   │   ├── llm.py                                # T4.6 / T3.8 (B12) — chat + stream
│   │   ├── rerank.py                             # T4.8  — P1 unit only, P2 wired
│   │   └── rate_limiter.py                       # T3.14 (B31) — Redis fixed-window per-key counter; powers chat /chat/stream Depends
│   ├── storage/minio_client.py                   # T2.6  (B10/B25/B28) — key-only return; bucket from MINIO_BUCKET
│   ├── workers/                                  # @broker.task modules — auto-imported by worker.py
│   │   ├── ingest.py                             # T3.2b (B16/B18) — `ingest.pipeline` task
│   │   └── supersede.py                          # T3.2d (P-C) — `ingest.supersede` task
│   ├── schemas/chat.py                           # T3.4  (B12/B21/B22/B29) — Pydantic ChatRequest
│   ├── errors/problem.py                         # T2.14 (B5) — RFC 9457 builder + error_code (§4.1.2)
│   ├── utility/
│   │   ├── id_gen.py                             # T0.4  — UUIDv7 → Crockford base32 (26 char)
│   │   └── datetime.py                           # T0.6  — UTC + ISO-Z helpers
│   └── state_machine.py                          # T0.7 (S10) — status transition rules; consumed by repo.update_status
└── tests/{conftest.py, unit/, integration/, e2e/}
```

**Module conventions:** No env reads outside `bootstrap/composition.py`. `@broker.task` decorators import from `ragent.bootstrap.broker` only. Plugins never import `pipelines/`, `routers/`, or HTTP layers. Routers → services → repositories (constructor-injected). `bootstrap/init_schema.py` applies `schema.sql` + `resources/es/*.json` idempotently; no inline DDL.
