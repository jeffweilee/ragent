# 00_plan.md — Master TDD Implementation Checklist

> Source: `docs/00_spec.md` · Authored: 2026-05-03 · Reorg: 2026-05-04 (Round 3)
> Workflow: `CLAUDE.md` §THE TDD WORKFLOW · Tidy First (`[STRUCTURAL]` / `[BEHAVIORAL]`)
> Each `[ ]` = one Red→Green→Refactor cycle, each cycle = one (or two) commit.
> Reorg driver: `docs/team/2026_05_04_phase1_round3_reorg_auth_off.md` (12/12 6-of-6).

## Status legend
- `[x]` delivered
- `[ ]` TODO
- `[~]` scaffolded-but-disabled (P1 OPEN mode — auth track deferred to P2)

---

## Phase 1 — Tracks (organized by domain)

> Tasks are grouped by domain track. The `Week` column preserves the original 5–7-week schedule for PM. The `Depends On` column lists prior task IDs that must be `[x]` first.

### Track T0 — Foundations (utilities & state machine)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T0.1 | Structural | Scaffold project: `pyproject.toml`, `src/ragent/`, `tests/{unit,integration,e2e}/`. | — | [x] | Dev | W1 |
| T0.2 | Structural | CI alias `make check` = `ruff format . && ruff check . --fix && pytest --cov=src/ragent --cov-branch --cov-fail-under=92`. Coverage floor enforced (DoD); fails CI on drop. | T0.1 | [x] | Dev | W1 |
| T0.3 | Red | `tests/unit/test_id_gen.py` — `new_id()` returns 26-char Crockford base32; sortable across calls. | T0.1 | [x] | QA | W2 |
| T0.4 | Green | `src/ragent/utility/id_gen.py` (UUIDv7 → 16 bytes → base32; ≤ 30 LOC). | T0.3 | [x] | Dev | W2 |
| T0.5 | Red | `tests/unit/test_datetime_utility.py` — `utcnow()` tz-aware UTC; `to_iso` ends in `Z`; `from_db` attaches UTC. | T0.1 | [x] | QA | W2 |
| T0.6 | Green | `src/ragent/utility/datetime.py`. | T0.5 | [x] | Dev | W2 |
| T0.7 | Red | `tests/unit/test_state_machine.py` — accepts `{UPLOADED→PENDING, PENDING→READY, PENDING→FAILED, PENDING→DELETING, READY→DELETING, FAILED→DELETING}`; rejects `{UPLOADED→FAILED, READY→PENDING, FAILED→READY, DELETING→READY}` (S10). | T0.1 | [x] | QA | W2 |
| T0.8 | Structural | DB migration `migrations/001_initial.sql` — `documents` (cols: `document_id CHAR(26) PK`, `create_user VARCHAR(64) NOT NULL` (B14), `source_id VARCHAR(128) NOT NULL` (B11), `source_app VARCHAR(64) NOT NULL` (B11), `source_title VARCHAR(256) NOT NULL` (B11), `source_workspace VARCHAR(64) NULL` (B11), `object_key VARCHAR(256) NOT NULL` (B25), `status ENUM(...)`, `attempt INT`, `created_at`, `updated_at`) + `chunks` (cols: `chunk_id`, `document_id`, `ord`, `text MEDIUMTEXT`, `lang VARCHAR(8)`); indexes `idx_status_updated`, `idx_source_app_id_status_created`, `idx_create_user_document`, `idx_document` (chunks). Alembic configured (B3). | T0.1 | [x] | Dev | W2 |
| T0.8a | Structural | `migrations/schema.sql` — consolidated snapshot reflecting head; updated in lockstep with every `NNN_*.sql` (B3). | T0.8 | [x] | Dev | W2 |
| T0.8e | Structural | Check in `resources/es/chunks_v1.json` — full settings + mappings per spec §5.2 (icu_text analyzer, bbq_hnsw vector index, B26). | T0.1 | [x] | Dev | W2 |
| T0.9 | Structural | `tests/conftest.py` — session-scoped `testcontainers-python` fixtures for MariaDB / ES / Redis / MinIO (B8). **ES container uses a custom image with `analysis-icu` plugin pre-installed** (e.g. `Dockerfile.es-test` extending `docker.elastic.co/elasticsearch/elasticsearch:9.2.3` with `bin/elasticsearch-plugin install --batch analysis-icu`); fixture exposes `ES_HOSTS`, `ES_USERNAME`, `ES_PASSWORD` (or `ES_API_KEY`) and `ES_VERIFY_CERTS=false` for self-signed dev (B28). MinIO fixture exposes `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_SECURE=false` (B28). MariaDB fixture builds `MARIADB_DSN` (B28). Redis container starts in standalone mode by default; sentinel topology covered by a separate compose-style fixture when `REDIS_MODE=sentinel` integration runs (B27). Reused by all `tests/integration/`. | T0.1 | [x] | Dev | W2 |
| T0.8b | Red | `tests/integration/test_schema_drift.py` — apply `schema.sql` and `alembic upgrade head` to two scratch DBs (testcontainers); `mysqldump` diff must be empty (B3 invariant). | T0.8a, T0.9 | [x] | QA | W2 |
| T0.8f | Red | `tests/integration/test_es_resource_drift.py` — parse `resources/es/chunks_v1.json` and the JSON block in spec §5.2; assert deep-equal (B26). Prevents prose/resource drift. Pure unit-style file diff; no testcontainer needed. | T0.8e | [x] | QA | W2 |
| T0.8c | Red | `tests/integration/test_bootstrap_auto_init.py` — first boot against empty MariaDB + ES creates tables/indexes/`chunks_v1` index idempotently using `resources/es/chunks_v1.json` (B26); second boot is a no-op; pre-existing schema drift is logged `event=schema.drift` and surfaces in `/readyz` as degraded (B3, B4). | T0.8a, T0.8e, T0.9 | [x] | QA | W2 |
| T0.8d | Green | `src/ragent/bootstrap/init_schema.py` — `CREATE … IF NOT EXISTS` for MariaDB; for ES, read every `resources/es/*.json` and `PUT /<index>` if absent (B26); refuses to ALTER. | T0.8c | [x] | Dev | W2 |
| T0.8g | Red | `tests/integration/test_es_plugin_required.py` — boot against an ES cluster lacking `analysis-icu` plugin → `/readyz` returns 503 with `error_code=ES_PLUGIN_MISSING` listing `analysis-icu`; init refuses to `PUT` the index (B26, I5). | T0.8d, T0.9 | [x] | QA | W2 |
| T0.10 | Structural | `src/ragent/bootstrap/broker.py` — TaskIQ `broker` instance (module-level export). Dispatches on `REDIS_MODE` (B27/B28): standalone reads `REDIS_BROKER_URL`; sentinel uses `REDIS_SENTINEL_HOSTS` + `REDIS_BROKER_SENTINEL_MASTER`. Single canonical import path `from ragent.bootstrap.broker import broker` used by every `@broker.task` decorator (T3.2b, T3.2d) and by the worker entrypoint (T7.5d). | T0.1 | [x] | Dev | W2 |
| T0.10a | Red | `tests/unit/test_broker_topology.py` — with `REDIS_MODE=standalone` the broker module constructs against `REDIS_BROKER_URL`; with `REDIS_MODE=sentinel` it constructs against `REDIS_SENTINEL_HOSTS` + `REDIS_BROKER_SENTINEL_MASTER` (B27); missing var in sentinel mode → SystemExit. | T0.10 | [x] | QA | W2 |
| T0.11 | Structural | `.env.example` at repo root enumerating **every** variable from spec §4.6 in the same subsection order (bootstrap → datastore → redis → clients → worker → pipeline → timeouts → observability). Required vars carry `# REQUIRED — set before boot`; default-overridable vars show their default value commented in. Operator workflow: `cp .env.example .env` → fill required → start API + worker. | T0.1 | [x] | Dev | W2 |
| T0.11a | Red | `tests/unit/test_env_example_drift.py` — parse spec §4.6 tables and `.env.example`; assert (a) symmetric variable set (no var in spec missing from example, no orphan in example), (b) defaults match where declared, (c) every `(required)` var in spec is marked `# REQUIRED` in example. CI gate against operator-config drift (B30). | T0.11 | [x] | QA | W2 |

### Track T1 — Plugins (Protocol + Registry + Extractors)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T1.1 | Red | `tests/unit/test_plugin_protocol.py` — protocol attribute/method conformance (S4). | T0.1 | [x] | QA | W2 |
| T1.2 | Green | `src/ragent/plugins/protocol.py` (`runtime_checkable` Protocol). | T1.1 | [x] | Dev | W2 |
| T1.3 | Red | Stub graph extractor no-op test (S5). | T0.1 | [x] | QA | W2 |
| T1.4 | Green | `src/ragent/plugins/stub_graph.py`. | T1.3 | [x] | Dev | W2 |
| T1.5 | Refactor | Reviewed: no shared boilerplate; kept duplicated per YAGNI. | T1.4 | [x] | Reviewer | W2 |
| T1.6 | Red | `tests/unit/test_plugin_registry.py` — register, fan_out, all_required_ok; duplicate name raises (S11); per-plugin timeout `PLUGIN_FAN_OUT_TIMEOUT_SECONDS` (B28, default 60) overrun → `Result(error="timeout")` (R6, S29). | T1.2 | [x] | QA | W3 |
| T1.7 | Green | `src/ragent/plugins/registry.py` (`PluginRegistry`, `Result`, `DuplicatePluginError`); concurrent fan_out with per-plugin timeout from `PLUGIN_FAN_OUT_TIMEOUT_SECONDS`. | T1.6 | [x] | Dev | W3 |
| T1.8 | Red | `tests/unit/test_plugin_registry_delete.py` — `fan_out_delete` calls every registered plugin; per-plugin timeout `PLUGIN_FAN_OUT_TIMEOUT_SECONDS` (B28); idempotent on already-deleted; runs with no DB tx open (R10, P-E). | T1.7 | [x] | QA | W3 |
| T1.9 | Red | `tests/unit/test_vector_extractor.py` — Protocol conformance, embedder/ES bulk once, idempotent rerun, delete clears chunks. | T1.2 | [x] | QA | W3 |
| T1.10 | Green | `src/ragent/plugins/vector.py`. | T1.9 | [x] | Dev | W3 |
| T1.11 | Red | `tests/unit/test_vector_extractor_title.py` — **B15 + B17 + B29 amendment.** Constructor signature `VectorExtractor(repo, chunks, embedder, es)` (B17 DI). Mock embedder captures the input string per chunk and asserts it equals `f"{source_title}\n\n{chunk_text}"`; ES bulk body for each chunk carries `title`, `text`, `lang`, `chunk_id`, `document_id`, `embedding`, `source_app`, `source_workspace` per `resources/es/chunks_v1.json` (B26, B29) — no extra fields, no missing fields. `source_workspace` is omitted from the bulk row when the document's column is NULL (ES `keyword` handles missing values; `term` filter on the field will not match those rows). `extract(document_id)` reads all four denormalised values via `repo.get(document_id)`. Constructor injection only — no service-locator, no globals. | T1.10, T2.2, T0.8e | [x] | QA | W3+ |
| T1.12 | Green | Amend `src/ragent/plugins/vector.py` — add constructor `(repo, chunks, embedder, es)` per B17; `extract` fetches doc, prepends `f"{source_title}\n\n"` to chunk text before embedding, writes `title`, `source_app`, `source_workspace` (when non-null) into the ES bulk doc (B29). | T1.11 | [x] | Dev | W3+ |

### Track T2 — Ingest CRUD (Repositories + Storage + Service + Router)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T2.1 | Red  | `tests/unit/test_document_repository.py` — `create (mandatory source_id + source_app + source_title + create_user + object_key, optional source_workspace — B11, B14, B25) / get(document_id) → DocumentRow with all of (document_id, create_user, source_id, source_app, source_title, source_workspace, object_key, status, attempt, created_at, updated_at) — B17 + B29 require source_app/source_workspace/source_title for VectorExtractor / acquire_nowait (FOR UPDATE NOWAIT raises on lock contention — R7, S28) / update_status (state-machine guarded) / update_heartbeat(document_id) → bumps updated_at to NOW() (B16, S33) / list_pending_stale(updated_at < NOW()-stale, attempt_le) (R1, B16) / list_uploaded_stale (R1, B16) / list / delete / list_ready_by_source(source_id, source_app) FOR UPDATE SKIP LOCKED / pop_oldest_loser_for_supersede(source_id, source_app) FOR UPDATE SKIP LOCKED LIMIT 1 (P-C single-loser-per-tx) / find_multi_ready_groups (R3) / get_sources_by_document_ids(ids) → {document_id: (source_app, source_id, source_title)} for chat hydration (B11, B12, C4 rename) / list_by_create_user(create_user, after, limit) future "my uploads" path (B14)`. | T0.4, T0.6, T0.7 | [ ] | QA | W3 |
| T2.2 | Green | `src/ragent/repositories/document_repository.py` (Repository layer; CRUD only). | T2.1 | [ ] | Dev | W3 |
| T2.3 | Red  | `tests/unit/test_chunk_repository.py` — `bulk_insert / delete_by_document_id`. | T0.4 | [ ] | QA | W3 |
| T2.4 | Green | `src/ragent/repositories/chunk_repository.py`. | T2.3 | [ ] | Dev | W3 |
| T2.5 | Red  | `tests/unit/test_minio_client.py` — `put_object(source_app, source_id, document_id, ...)` builds key `{source_app}_{source_id}_{document_id}` (sanitised to `[A-Za-z0-9._-]`) in bucket from `MINIO_BUCKET` env (default `ragent`, B10/B28); upload bound by `MINIO_PUT_TIMEOUT_SECONDS` (B28, default 60), download by `MINIO_GET_TIMEOUT_SECONDS` (B28, default 30); **returns the object key only, not a URI** (B25, C3); `delete_object(key)` idempotent. Bucket name is read once at startup, never per-row. MinIO is transient staging only — cleared on terminal pipeline state. | T0.1 | [ ] | QA | W3 |
| T2.6 | Green | `src/ragent/storage/minio_client.py` (B10 key format, B25 returns key string; per-call timeouts wired from `MINIO_PUT_TIMEOUT_SECONDS` / `MINIO_GET_TIMEOUT_SECONDS`, B28). | T2.5 | [ ] | Dev | W3 |
| T2.7 | Red  | `tests/unit/test_ingest_service_create.py` — validate `source_id`+`source_app`+`source_title` mandatory (S23 → 422 on missing/empty — B11), MIME validate against C1 allow-list `{text/plain, text/markdown, text/html, text/csv}` (≤50 MB) → put → repo.create (persists `source_id`, `source_app`, `source_title`, `object_key` per B25, `create_user`, optional `source_workspace`) → kiq dispatch; rolls back row if MinIO put fails. | T2.2, T2.6, T1.7 | [ ] | QA | W3 |
| T2.8 | Green | `src/ragent/services/ingest_service.py::create` (≤ 30 LOC/method). | T2.7 | [ ] | Dev | W3 |
| T2.9 | Red  | `tests/unit/test_ingest_service_delete.py` — cascade order (acquire NOWAIT → DELETING short tx → fan_out_delete OUTSIDE tx → chunks → MinIO if UPLOADED/PENDING → row); on any failure row stays DELETING (S13); idempotent re-delete returns 204 (S14); fan_out_delete runs with no DB tx open (P-E). | T2.8, T1.8 | [ ] | QA | W3 |
| T2.10 | Green | `src/ragent/services/ingest_service.py::delete`. | T2.9 | [ ] | Dev | W3 |
| T2.11 | Red | `tests/unit/test_ingest_service_list.py` — cursor pagination by `document_id` ASC; `?limit` honours upper bound `INGEST_LIST_MAX_LIMIT` (B28, default 100) — request with `limit=200` is clamped or 422 per service contract; `next_cursor` correctness (S15, P1 OPEN: no ACL filter). | T2.2 | [ ] | QA | W3 |
| T2.12 | Green | `src/ragent/services/ingest_service.py::list`. | T2.11 | [ ] | Dev | W3 |
| T2.13 | Red | `tests/unit/test_ingest_router.py` — Router only parses/validates and delegates; 415 (`error_code=INGEST_MIME_UNSUPPORTED`) on MIME outside C1 allow-list, 413 (`INGEST_FILE_TOO_LARGE`) on body > `INGEST_MAX_FILE_SIZE_BYTES` (B28, default 50 MB / 52428800); 422 on missing/empty `source_id` / `source_app` / `source_title` (S23, B11); `X-User-Id` required (P1 OPEN). All non-2xx bodies are RFC 9457 `application/problem+json` with `error_code` (B5); 422 carries `errors[]`. | T2.8, T2.10, T2.12 | [ ] | QA | W3 |
| T2.14 | Green | `src/ragent/routers/ingest.py` + `src/ragent/errors/problem.py` (RFC 9457 builder, B5). Declares all endpoints in spec §4.1. | T2.13 | [ ] | Dev | W3 |

### Track T3 — Pipelines (Ingest + Chat assembly)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T3.1 | Red | `tests/integration/test_ingest_pipeline.py` — Haystack `Convert→Clean→LanguageRouter→{cjk_splitter\|en_splitter} (sentence)→Embed` (B1); mock embedder. EN doc routed to `en_splitter`; CJK doc routed to `cjk_splitter`; chunk count == sentence count. | T2.4, T4.2 | [ ] | QA | W3 |
| T3.2 | Green | `src/ragent/pipelines/factory.py` + `pipelines/ingest.py`. | T3.1 | [ ] | Dev | W3 |
| T3.2a | Red | `tests/integration/test_worker_minio_cleanup.py` — terminal status (`READY` or `FAILED`) is committed **before** `MinIOClient.delete_object` is called; if the delete raises, the document still ends up in the terminal status and an `event=minio.orphan_object` log is emitted (S16, S21). On retry path (still `PENDING`), MinIO object is retained. | T3.2 | [ ] | QA | W3 |
| T3.2b | Green | Worker task `@broker.task("ingest.pipeline")` (broker imported from `ragent.bootstrap.broker`, T0.10): TX-A acquire NOWAIT + PENDING (commit) → **start heartbeat timer (B16): UPDATE updated_at every WORKER_HEARTBEAT_INTERVAL_SECONDS** → pipeline body OUTSIDE tx, wrapped in `asyncio.wait_for(timeout=PIPELINE_TIMEOUT_SECONDS)` (B18) → cancel heartbeat in `finally` → TX-B terminal commit (FAILED branch also runs fan_out_delete + delete_by_document_id per R5, S27; PIPELINE_TIMEOUT branch sets `error_code=PIPELINE_TIMEOUT`) → post-commit MinIO best-effort (`MinIOClient.delete_object(object_key)`, B25). Plugin registry + repos resolved via composition root (T7.5a). | T3.2a, T0.10 | [ ] | Dev | W3 |
| T3.2i | Red | `tests/integration/test_worker_heartbeat.py` — Given a worker is mid-pipeline (mock 4-min sleep), When **a stand-in Reconciler query** runs at 5 min wall-clock (`list_pending_stale(now - RECONCILER_PENDING_STALE_SECONDS)`, B28), Then it observes `updated_at < 30 s` ago and the worker row is **not** in the result set (S33, B16). When the worker is killed, `updated_at` ages past 5 min and the same query returns the row exactly once. (Full Reconciler entrypoint behaviour covered in T5.) | T3.2b, T2.2 | [ ] | QA | W3 |
| T3.2j | Red | `tests/integration/test_pipeline_timeout.py` — Given a pipeline body that sleeps > `PIPELINE_TIMEOUT_SECONDS`, When the timeout fires, Then row → `FAILED` with `error_code=PIPELINE_TIMEOUT`, cleanup ran, `event=ingest.failed reason=pipeline_timeout` logged (S34, B18). | T3.2b | [ ] | QA | W3 |
| T3.2k | Red | `tests/integration/test_csv_row_merger.py` — 10 000-row CSV (~50 chars each); after ingest, `chunks` row count ≤ `ceil(total_chars / CSV_CHUNK_TARGET_CHARS)` (≈ 250); a `.txt` of identical size produces sentence-level chunks (RowMerger bypassed) — verifies `ConditionalRouter` on MIME (S35, B24). | T3.2 | [ ] | QA | W3 |
| T3.2l | Green | Pipeline factory adds `RowMerger` SuperComponent on the `text/csv` branch only (`ConditionalRouter` keyed on MIME); merges rows joined by `\n` until buffer ≥ `CSV_CHUNK_TARGET_CHARS` (B24). | T3.2k | [ ] | Dev | W3 |
| T3.2c | Red | `tests/integration/test_supersede_task.py` — on `READY`, kiq `ingest.supersede`; task pops oldest loser one-at-a-time and commits per-loser (P-C, S31), keeps `MAX(created_at)`, cascade-deletes the rest (S17); same `source_id` with different `source_app` coexists (S22); out-of-order finish still converges to MAX(created_at) survivor (S20); FAILED never enqueues supersede (S18); idempotent re-run (S19). | T3.2b, T2.10 | [ ] | QA | W3 |
| T3.2d | Green | `src/ragent/services/ingest_service.py::supersede(document_id)` + TaskIQ `@broker.task("ingest.supersede")` worker (broker from T0.10); loops `pop_oldest_loser_for_supersede` + cascade delete + commit; never holds K row locks across K cascades. | T3.2c, T0.10 | [ ] | Dev | W3 |
| T3.2e | Red | `tests/integration/test_pipeline_retry_idempotent.py` — Reconciler retry of a partially-written ingest produces no duplicate chunks; pipeline first step is `delete_by_document_id` + `VectorExtractor.delete` (R4, S25). | T3.2 | [ ] | QA | W3 |
| T3.2f | Green | Pipeline factory prepends idempotency-clean step. | T3.2e | [ ] | Dev | W3 |
| T3.2g | Red | `tests/unit/test_worker_acquire_nowait.py` — concurrent dispatch: second worker fails fast on `FOR UPDATE NOWAIT`, re-kiqs self with backoff, does **not** increment `attempt` (R7, S28). | T3.2 | [ ] | QA | W3 |
| T3.2h | Green | Worker uses `acquire_nowait`; on `LockNotAvailable` exception, re-kiq with exponential backoff (cap 30 s). | T3.2g | [ ] | Dev | W3 |
| T3.3 | Red | `tests/unit/test_chat_request_schema.py` — request validation (B12, S6c): `messages` required (422 `error_code=CHAT_MESSAGES_MISSING` if missing/empty); defaults from env vars (`RAGENT_DEFAULT_LLM_PROVIDER/MODEL/TEMPERATURE/MAX_TOKENS`); `provider` validated against allow-list `{"openai"}` else 422 `CHAT_PROVIDER_UNSUPPORTED` (B22); auto-prepend `{"role":"system","content":"<RAGENT_DEFAULT_SYSTEM_PROMPT>"}` if no system entry (S6b, observable via mock LLM capture). **Filter fields (B29, S6i):** `source_app` / `source_workspace` are optional; both omitted ⇒ no filter; empty string or > 64 chars ⇒ 422 `CHAT_FILTER_INVALID`. | T4.6 | [ ] | QA | W4 |
| T3.4 | Green | `src/ragent/schemas/chat.py` (Pydantic) — `ChatRequest` with env-driven defaults + `provider` allow-list validator (B22) + optional `source_app`/`source_workspace` filter fields (B29, length 1–64, snake_case per B21); `Message`; `normalize_messages()` helper that prepends default system. JSON field naming per B21 (`maxTokens` camelCase; ID/scope fields snake_case). | T3.3 | [ ] | Dev | W4 |
| T3.5 | Red | `tests/integration/test_chat_pipeline_retrieval.py` — QueryEmbedder (timeout `EMBEDDER_QUERY_TIMEOUT_SECONDS`, B28) → {ESVector kNN on `embedding` (`bbq_hnsw`, B26) + ESBM25 `multi_match(["text","title^2"])` with `icu_text` analyzer (B26) sequential, B15} both retrievers timeout `ES_QUERY_TIMEOUT_SECONDS` (B28, default 10) → `DocumentJoiner` configured by `CHAT_JOIN_MODE` env (C6) → `SourceHydrator` joins `documents` for `(source_app, source_id, source_title)` (B11) and **truncates `excerpt` to `EXCERPT_MAX_CHARS`** (B23) → returns hydrated chunks. Title-only lexical query recalls via BM25 `title^2`; semantic title query (synonym of title) recalls via the title-aware `embedding` (B15). **CJK BM25 (S36, B26):** Chinese-text query (e.g. `"產品規格"` against a doc body containing the same term with no whitespace) recalls via `icu_tokenizer`; the same query against a `standard`-analyzed control index does not — proves the analyzer choice matters. **Filter (B29, S6f/S6g/S6h):** when `ChatRequest.source_app` is set, both retrievers carry `term: {source_app: <v>}` in their `filter` clause and only matching chunks reach the joiner; combined `source_app + source_workspace` ANDs; non-matching filter ⇒ empty hydrated list (router maps to `sources: null`, S6h). Empty index → hydrated list is empty. | T2.4, T4.2, T4.4, T2.2, T1.12 | [ ] | QA | W4 |
| T3.5a | Red | `tests/unit/test_pipeline_factory_join_mode.py` — for each value of `CHAT_JOIN_MODE` ∈ {`rrf`, `concatenate`, `vector_only`, `bm25_only`} the factory returns a pipeline with the expected component graph (e.g. `vector_only` has no BM25 retriever and no joiner). Default is `rrf` (C6). | T3.5 | [ ] | QA | W4 |
| T3.6 | Green | `src/ragent/pipelines/chat.py::build_retrieval_pipeline(join_mode)` + `SourceHydrator` component (calls `DocumentRepository.get_sources_by_document_ids`, C4 rename; truncates `excerpt` to `EXCERPT_MAX_CHARS`, B23). Factory dispatches on `CHAT_JOIN_MODE` env (C6). | T3.5, T3.5a | [ ] | Dev | W4 |
| T3.7 | Red | `tests/unit/test_llm_client_chat.py` — `LLMClient.chat(messages,...)` non-streaming returns `{content, usage:{promptTokens,completionTokens,totalTokens}}`; honours `model`, `temperature`, `maxTokens`; timeout `LLM_TIMEOUT_SECONDS` (B28, default 120). | T4.6 | [ ] | QA | W4 |
| T3.8 | Green | Add `LLMClient.chat()` non-streaming method to `src/ragent/clients/llm.py` (timeout from `LLM_TIMEOUT_SECONDS`); existing `stream()` already covered by T4.5/T4.6. Streaming path requests `stream_options.include_usage=true` so terminal `done` carries usage. | T3.7 | [ ] | Dev | W4 |
| T3.9 | Red | `tests/integration/test_chat_endpoint.py` — `POST /chat` (B12, S6a): returns `200 application/json` with §3.4.2 body (`content`, `usage`, `model`, `provider`, `sources[]`). Each `sources[]` entry has all 6 fields populated and `type="knowledge"` (S6e, B13). Empty retrieval → `sources: null` (S6d). On LLM error → RFC 9457 problem+json (B5). | T3.6, T3.8 | [ ] | QA | W4 |
| T3.10 | Green | `src/ragent/routers/chat.py::POST /chat` (non-streaming). | T3.9 | [ ] | Dev | W4 |
| T3.11 | Red | `tests/integration/test_chat_stream_endpoint.py` — `POST /chat/stream` (B12, S6): emits ≥1 `data: {"type":"delta","content":"..."}` then exactly one `data: {"type":"done", ...§3.4.2 body...}` (same shape as T3.9 response). Mid-stream LLM failure after first delta → single `data: {"type":"error","error_code","message"}` then close; no `event: error` named-event (B6). Pre-stream failure → RFC 9457 problem+json (B5). | T3.6, T3.8 | [ ] | QA | W4 |
| T3.12 | Green | `src/ragent/routers/chat.py::POST /chat/stream` — SSE writer with delta/done/error framing per §3.4.3. | T3.11 | [ ] | Dev | W4 |
| T3.13 | Red | `tests/unit/test_rate_limiter.py` — fixed-window per-key counter against testcontainer Redis (B31): `check(key, limit, window_seconds)` returns `RateLimitResult(allowed, remaining, reset_at)`. Under limit ⇒ allowed; at limit ⇒ blocked + `reset_at = now + remaining_window`; window expiry resets counter (subsequent call allowed). Uses `INCR` + `EXPIRE` against `REDIS_RATELIMIT_*` topology (B27); standalone vs sentinel both covered. | T0.10, T0.9 | [ ] | QA | W4 |
| T3.14 | Green | `src/ragent/clients/rate_limiter.py` — `RateLimiter` adapter; reads `REDIS_RATELIMIT_URL` (standalone) or `REDIS_RATELIMIT_SENTINEL_MASTER` + `REDIS_SENTINEL_HOSTS` (sentinel) from composition root (T7.5a); key prefix `ratelimit:`. Composition root exports `chat_rate_limit_dep` FastAPI `Depends` factory bound to `CHAT_RATE_LIMIT_PER_MINUTE` / `CHAT_RATE_LIMIT_WINDOW_SECONDS` (B31). | T3.13 | [ ] | Dev | W4 |
| T3.15 | Red | `tests/integration/test_chat_rate_limit.py` — with `CHAT_RATE_LIMIT_PER_MINUTE=N`, **N+1** `POST /chat` calls from same `X-User-Id` within the window: first N return 200, the (N+1)th returns 429 `application/problem+json` with `error_code=CHAT_RATE_LIMITED` + `Retry-After` header (seconds until window reset, B31, S37); same applies to `/chat/stream`. Different `X-User-Id` gets a fresh budget (per-user isolation). After window expiry (`time.sleep(window+ε)` against fake clock), counter resets. | T3.14, T3.10, T3.12 | [ ] | QA | W4 |
| T3.16 | Green | Wire `Depends(chat_rate_limit_dep)` onto the chat router (T3.10/T3.12) — applies to both `/chat` and `/chat/stream`. Health endpoints, ingest, and MCP are NOT rate-limited (router-level dependency, not global middleware, B31). | T3.15 | [ ] | Dev | W4 |

### Track T4 — Third-Party Clients

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T4.1 | Red | `tests/unit/test_token_manager.py` — POSTs `AI_API_CLIENT_ID` + `AI_API_CLIENT_SECRET` (B28) to `AI_API_AUTH_URL/auth/api/accesstoken`; refreshes at `expiresAt − 5min` boundary using fake clock (S9); single-flight refresh: 100 concurrent calls share one HTTP exchange (P-F). Credentials never appear in log records or error responses. | T0.6 | [ ] | QA | W4 |
| T4.2 | Green | `src/ragent/clients/auth.py` (`TokenManager`) with `asyncio.Lock` / `threading.Lock` around exchange. | T4.1 | [ ] | Dev | W4 |
| T4.3 | Red | `tests/unit/test_embedding_client.py` — POST shape, `bge-m3`, validates `returnCode == 96200`, retry 3× @ 1 s; **batch interface** accepts `list[str]` and issues one HTTP call per batch up to `EMBEDDER_BATCH_SIZE` (B28, default 32, P-B); ingest call bound by `EMBEDDER_INGEST_TIMEOUT_SECONDS` (B28, default 30); single-string query call bound by `EMBEDDER_QUERY_TIMEOUT_SECONDS` (B28, default 10) — asymmetric per C8. | T4.2 | [ ] | QA | W4 |
| T4.4 | Green | `src/ragent/clients/embedding.py` (batch size from `EMBEDDER_BATCH_SIZE`, two timeout call paths from `EMBEDDER_INGEST_TIMEOUT_SECONDS` / `EMBEDDER_QUERY_TIMEOUT_SECONDS`). | T4.3 | [ ] | Dev | W4 |
| T4.5 | Red | `tests/unit/test_llm_client.py` — streaming async iterator yields deltas; timeout `LLM_TIMEOUT_SECONDS` (B28, default 120); retry 3× @ 2 s. | T4.2 | [ ] | QA | W4 |
| T4.6 | Green | `src/ragent/clients/llm.py` (timeout from `LLM_TIMEOUT_SECONDS`). | T4.5 | [ ] | Dev | W4 |
| T4.7 | Red | `tests/unit/test_rerank_client.py` — POST shape, `bge-reranker-base`, `top_k=2`. (Wired P2.) | T4.2 | [ ] | QA | W4 |
| T4.8 | Green | `src/ragent/clients/rerank.py`. | T4.7 | [ ] | Dev | W4 |

### Track T5 — Resilience (Reconciler)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T5.1 | Red | `tests/integration/test_reconciler_redispatch.py` — `PENDING` row with `updated_at < NOW() - RECONCILER_PENDING_STALE_SECONDS` (B16/B28, default 5 min) → re-kiq, idempotent (S2). Live worker with fresh `updated_at` is **not** re-dispatched (S33). | T2.2, T2.8 | [ ] | QA | W6 |
| T5.2 | Green | `src/ragent/reconciler.py` — one-shot entrypoint `python -m ragent.reconciler` (B9: K8s `CronJob` `*/5 * * * *`, `concurrencyPolicy: Forbid`); `SELECT … FOR UPDATE SKIP LOCKED`. Manifest `deploy/k8s/reconciler-cronjob.yaml` checked in. | T5.1 | [ ] | Dev | W6 |
| T5.3 | Red | `tests/integration/test_reconciler_failed.py` — `attempt > WORKER_MAX_ATTEMPTS` (B28, default 5) → status=FAILED + structured-log alert (S3). | T5.2 | [ ] | QA | W6 |
| T5.4 | Green | Status transition + structured log line `event=ingest.failed`. | T5.3 | [ ] | Dev | W6 |
| T5.5 | Red | `tests/integration/test_reconciler_delete_resume.py` — `DELETING` row with `updated_at < NOW() - RECONCILER_DELETING_STALE_SECONDS` (B28, default 300) → resume cascade idempotently (S13). | T2.10 | [ ] | QA | W6 |
| T5.6 | Green | Reconciler resumes DELETING. | T5.5 | [ ] | Dev | W6 |
| T5.7 | Red | `tests/integration/test_reconciler_uploaded_orphan.py` — `UPLOADED` row with `updated_at < NOW() - RECONCILER_UPLOADED_STALE_SECONDS` (B28, default 300) → re-kiq `ingest.pipeline` (R1, S24). | T2.8 | [ ] | QA | W6 |
| T5.8 | Green | Reconciler arm for `UPLOADED > 5 min`. | T5.7 | [ ] | Dev | W6 |
| T5.9 | Red | `tests/integration/test_reconciler_multi_ready_repair.py` — two READY rows for same `(source_id, source_app)` → re-enqueue `ingest.supersede` (R3, S26). | T3.2d | [ ] | QA | W6 |
| T5.10 | Green | Reconciler arm: `GROUP BY source_id, source_app HAVING COUNT(*)>1` → kiq supersede. | T5.9 | [ ] | Dev | W6 |
| T5.11 | Red | `tests/integration/test_reconciler_failed_cleanup.py` — on `attempt>5 → FAILED`, partial chunks/ES are cleared (R5, S27). | T5.4 | [ ] | QA | W6 |
| T5.12 | Green | FAILED transition runs `fan_out_delete` + `delete_by_document_id` before commit. | T5.11 | [ ] | Dev | W6 |
| T5.13 | Red | `tests/integration/test_reconciler_heartbeat.py` — every tick increments `reconciler_tick_total` and emits `event=reconciler.tick` (R8, S30). | T5.2 | [ ] | QA | W6 |
| T5.14 | Green | Heartbeat counter + log line in `reconciler.py`. | T5.13 | [ ] | Dev | W6 |

### Track T6 — MCP Schema (501 in P1)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T6.1 | Structural | `src/ragent/routers/mcp.py` exposing `POST /mcp/tools/rag` that returns 501 `application/problem+json` with `error_code=MCP_NOT_IMPLEMENTED` (§4.1.2). Mounted by `create_app` (T7.5c). OpenAPI auto-generated from FastAPI signature. | T2.14 | [ ] | Dev | W6 |
| T6.2 | Red | `tests/unit/test_mcp_endpoint.py` — returns 501 in P1 (S8). | T6.1 | [ ] | QA | W6 |

### Track T7 — Observability + Acceptance

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T7.1 | Refactor | Wire OTEL: Haystack auto-trace + FastAPI middleware (no custom spans). OTLP exporter configured from `OTEL_EXPORTER_OTLP_ENDPOINT` (B28; export disabled when unset), service name from `OTEL_SERVICE_NAME` per process (`ragent-api` / `ragent-worker` / `ragent-reconciler`), sampling from `OTEL_TRACES_SAMPLER` + `OTEL_TRACES_SAMPLER_ARG`. Logs include `auth_mode=open` field. Counters: `reconciler_tick_total`, `minio_orphan_object_total`, `multi_ready_repaired_total`. Histogram: `worker_pipeline_duration_seconds`. | T3.4 | [ ] | SRE | W6 |
| T7.1a | Red | `tests/integration/test_alerting_rules.py` — Prometheus rule fires when `reconciler_tick_total` flat for > 10 min (R8, S30). | T7.1 | [ ] | QA | W6 |
| T7.2 | Acceptance | **Quickstart E2E (B30):** `tests/e2e/test_ingest_success_rate.py` — fresh testcontainer environment + populated `.env` (from `.env.example`, T0.11) → launch real entrypoints `python -m ragent.api` (T7.5d) and `python -m ragent.worker` (T7.5e) as subprocesses (no test-only scaffolding) → POST 100 documents to `/ingest` → poll `/ingest/{id}` → ≥ 99% reach `READY` within 60 s each. Asserts the operator promise: **only steps required are `cp .env.example .env`, fill required vars, start API + worker.** | T3.2, T5.6, T7.5d, T7.5e, T0.11 | [ ] | QA | W6 |
| T7.3 | Acceptance | Golden 50-Q top-3 ≥ 70% (`tests/e2e/test_golden_set.py`) **invoked against the same launched API process as T7.2** (`POST /chat` against `http://127.0.0.1:8000/chat`); dataset asset checked in at `tests/e2e/golden_set.jsonl` (50 Q/A/expected_doc_id rows; C7, satisfies the W3 journal rule on measurement assets). | T3.4, T7.2 | [ ] | QA | W6 |
| T7.4 | Acceptance | Chaos: kill worker mid-ingest → Reconciler recovers ≤ 10 min. | T5.6 | [ ] | SRE | W6 |
| T7.5 | Structural | Startup guard module `src/ragent/bootstrap/guard.py` — refuses to start unless `RAGENT_AUTH_DISABLED=true AND RAGENT_ENV=dev` (B28); reads `RAGENT_HOST` (B28, default `127.0.0.1`) — guard refuses any non-loopback bind while in P1 OPEN — and `RAGENT_PORT` (B28, default 8000). Configures `LOG_LEVEL` (B28, default `INFO`) for app + TaskIQ + Reconciler logging. Imported by both api and worker entrypoints. | T2.14 | [ ] | SRE | W6 |
| T7.5a | Structural | `src/ragent/bootstrap/composition.py` — **composition root (B30)**: builds singletons `TokenManager`, `EmbeddingClient`, `LLMClient`, `RerankClient`, `MinIOClient`, `Elasticsearch`, MariaDB engine, `RateLimiter` (T3.14, B31); instantiates `DocumentRepository`, `ChunkRepository`; constructs plugin instances with B17 DI args (`VectorExtractor(repo, chunks, embedder, es)`, `StubGraphExtractor()`); registers all required + optional plugins onto a single `PluginRegistry`; builds the chat pipeline factory parameterised by `CHAT_JOIN_MODE` (C6); exports the `chat_rate_limit_dep` FastAPI `Depends` factory bound to the `RateLimiter` + `CHAT_RATE_LIMIT_PER_MINUTE` / `CHAT_RATE_LIMIT_WINDOW_SECONDS` (B31). Exposes a `Container` dataclass + `get_container()` accessor used by FastAPI `Depends` (T2.14, T3.10, T3.12, T7.8) and by worker tasks (T3.2b, T3.2d) and Reconciler (T5.2). All env-var reads happen here, not inside business modules. | T1.7, T1.12, T2.2, T2.4, T2.6, T3.6, T3.8, T3.14, T4.2, T4.4, T4.6, T4.8 | [ ] | Dev | W6 |
| T7.5b | Red | `tests/integration/test_composition_root.py` — `get_container()` against testcontainers builds a fully-wired graph: `container.registry.plugins["vector"]` is a `VectorExtractor` whose `__init__` received the same `repo`/`chunks`/`embedder`/`es` instances exposed elsewhere on the container (B17 DI verified by identity); `container.chat_pipeline_factory(join_mode="rrf")` returns a runnable Haystack pipeline; missing required env var → SystemExit at construction time, not lazy at first request. | T7.5a | [ ] | QA | W6 |
| T7.5c | Structural | `src/ragent/bootstrap/app.py::create_app() -> FastAPI` — calls `guard.enforce()` (T7.5), constructs the `Container` (T7.5a), registers a FastAPI `lifespan` hook that calls `init_schema()` (T0.8d) **before** the app accepts traffic, mounts routers (`ingest` T2.14, `chat` T3.10/T3.12, `mcp` T6.x, `health` T7.8), wires `Depends` from the container, installs error-handler middleware that emits RFC 9457 problem+json (B5) and the `X-User-Id` middleware that excludes `/livez`/`/readyz`/`/metrics` (C9). | T7.5, T7.5a, T0.8d, T2.14, T3.10, T3.12, T7.8 | [ ] | Dev | W6 |
| T7.5d | Structural | `src/ragent/api.py` (`python -m ragent.api`) — uvicorn launcher: imports `bootstrap.app:create_app`, binds to `RAGENT_HOST:RAGENT_PORT`, log level from `LOG_LEVEL`. **This is the only command an operator runs to start the API process.** | T7.5c | [ ] | Dev | W6 |
| T7.5e | Structural | `src/ragent/worker.py` (`python -m ragent.worker`) — TaskIQ worker entrypoint: calls `guard.enforce()` and constructs the same `Container` (T7.5a) so plugins/repos are available to tasks; imports `ragent.bootstrap.broker:broker` (T0.10) and the task modules (`ragent.workers.ingest`, `ragent.workers.supersede`) so `@broker.task` decorators register; runs the broker. Also calls `init_schema()` defensively (idempotent — same auto-init as the API process, T0.8d) so the worker can be started before the API process the first time. **This is the only command an operator runs to start the worker process.** | T0.10, T7.5a, T0.8d, T3.2b, T3.2d | [ ] | Dev | W6 |
| T7.5f | Red | `tests/integration/test_app_factory.py` — `create_app()` against testcontainers boots; FastAPI `lifespan` runs `init_schema` exactly once; routers mounted at expected paths; `/livez` reachable without `X-User-Id`; protected route returns 422 problem+json on missing `X-User-Id`. | T7.5c | [ ] | QA | W6 |
| T7.6 | Red | `tests/unit/test_bootstrap_startup_guard.py` — non-dev env or `RAGENT_AUTH_DISABLED` unset → SystemExit; `RAGENT_HOST` other than `127.0.0.1` while `RAGENT_AUTH_DISABLED=true` → SystemExit; `LOG_LEVEL` invalid value → SystemExit; happy path boots with all three vars at defaults. | T7.5 | [ ] | QA | W6 |
| T7.7 | Red | `tests/integration/test_health_endpoints.py` — (B4) `GET /livez` → 200 always; `GET /readyz` → 200 only when **all** probes pass: **MariaDB** (`SELECT 1`), **ES cluster health + `analysis-icu` plugin loaded** (B26) **+ every `resources/es/*.json` index exists**, **Redis broker** (`PING` against active topology per `REDIS_MODE`, B27), **Redis rate-limiter** (`PING`, separate logical instance, B27), **MinIO** (`ListBuckets`); each probe budget ≤ `READYZ_PROBE_TIMEOUT_SECONDS` (B28, default 2 s). **One dep down → 503 `application/problem+json`** with `error_code` matching the failure: `ES_PLUGIN_MISSING` when plugin absent, `ES_INDEX_MISSING` when a `resources/es/*.json` index missing, `SCHEMA_DRIFT` when MariaDB or ES schema diverges (§4.1.2 catalog). `GET /metrics` → 200 `text/plain; version=0.0.4` exposing `reconciler_tick_total`, `worker_pipeline_duration_seconds`, `minio_orphan_object_total`, `multi_ready_repaired_total`. **All three endpoints reachable WITHOUT `X-User-Id` header** (middleware bypass, C9). | T7.1 | [ ] | QA | W6 |
| T7.8 | Green | `src/ragent/routers/health.py` (`/livez`, `/readyz`, `/metrics`); readiness probes via `asyncio.wait_for(..., timeout=READYZ_PROBE_TIMEOUT_SECONDS)` per dep (B28); broker + rate-limiter Redis pinged independently per their `REDIS_*_URL` / `REDIS_*_SENTINEL_MASTER` (B27). **`X-User-Id` middleware excludes the three health paths** (C9). | T7.7 | [ ] | Dev | W6 |

### Track T8 — Authentication & Permission Layer `[~] DISABLED IN P1`

> P1 produces NO code in this track. Authentication (JWT) and Permission (OpenFGA via `PermissionClient` Protocol) are **separate layers**; ES carries no auth fields in any phase (B14). Interface in `00_spec.md` §3.5; implementation lands in P2 (see Phase 2 row P2.2).

| # | Category | Task | Depends On | Status | Owner | Phase |
|---|---|---|---|:---:|---|:---:|
| T8.1 | Red  | `tests/unit/test_jwt.py` — invalid → 401 problem+json on all `/chat*` and `/ingest*` endpoints. | (entry) | [~] | QA | P2 |
| T8.2 | Green | `src/ragent/auth/jwt.py` (FastAPI dependency); subject claim → `user_id`. | T8.1 | [~] | Dev | P2 |
| T8.3 | Red  | `tests/unit/test_permission_client_protocol.py` — Protocol conformance for `batch_check(user_id, document_ids, relation) -> set[str]` and `list_objects(user_id, relation) -> list[str] \| None`. | T8.2 | [~] | QA | P2 |
| T8.4 | Green | `src/ragent/auth/permission.py` — `PermissionClient` Protocol + `OpenFGAPermissionClient` implementation (sole module importing the OpenFGA SDK; B14). | T8.3 | [~] | Dev | P2 |
| T8.5 | Red  | `tests/integration/test_chat_permission_gate.py` — chat retrieval over-fetches K' candidates; `PermissionClient.batch_check` post-filters to allowed; user A's query never surfaces a chunk whose `document_id` belongs to user B's private doc. ES query body asserted to carry NO auth filter (B14). | T8.4 | [~] | QA | P2 |
| T8.6 | Green | Wire `PermissionClient` post-retrieval gate into `pipelines/chat.py` (between SourceHydrator and LLM, or before hydration). | T8.5 | [~] | Dev | P2 |
| T8.7 | Red  | `tests/unit/test_ingest_permission.py` — `GET /ingest/{id}` and `DELETE /ingest/{id}` call `PermissionClient.batch_check([id])` and 403 problem+json when not allowed. | T8.4 | [~] | QA | P2 |
| T8.8 | Green | Wire `PermissionClient` into `services/ingest_service.py::get/delete`; `list` either calls `list_objects` then `WHERE document_id IN (...)`, or falls back to `batch_check` per page when `list_objects` returns `None`. | T8.7 | [~] | Dev | P2 |
| T8.9 | Behavioral | `HRClient` + JWT-subject → employee resolution; OpenFGA tuple-write on ingest (`user:<id>` viewer of `document:<doc_id>`). | T8.2, T8.4 | [~] | Dev | P2 |

---

## Definition of Done — Phase 1

- [ ] Every `[ ]` row in tracks T0–T7 is `[x]`.
- [ ] T8 rows remain `[~]` (auth disabled by design); spec §3.5 and §4.5 still describe the P2 contract.
- [ ] `uv run ruff format . && uv run ruff check . && uv run pytest` exits 0.
- [ ] **TDD test coverage > 92%** — `uv run pytest --cov=src/ragent --cov-fail-under=92` passes; CI rejects PRs that drop below the floor. Coverage is line + branch.
- [ ] Acceptance metrics T7.2 / T7.3 / T7.4 met.
- [ ] Startup guard (T7.5) verified (`pytest tests/unit/test_bootstrap_startup_guard.py`).
- [ ] Every BDD scenario in `00_spec.md` §3.X has a corresponding plan row whose test path matches.
- [ ] `00_journal.md` carries at least one P1 lesson per domain encountered.

---

## Phase 2 — Production Quality (+3 weeks) — *not started*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P2.1 | Stability | SRE: HA verification, monitoring, alerting rules. | [ ] | SRE |
| P2.2 | Security | Activate JWT auth + Permission Layer (`PermissionClient` over OpenFGA) per Track T8 (all `[~]` rows → `[ ]` → `[x]`); B14: ES still carries no auth fields. Remove `RAGENT_AUTH_DISABLED` env knob. | [ ] | Dev |
| P2.3 | Behavioral | Wire `RerankClient` into chat pipeline as `HybridRetrieverWithRerank` SuperComponent. | [ ] | Dev |
| P2.4 | Behavioral | `ConditionalRouter` intent split (translate/summarize → Direct LLM). | [ ] | Dev |
| P2.5 | Behavioral | MCP Tool real handler. | [ ] | Dev |
| P2.6 | Quality | RAGAS eval in CI; large-file streaming; chaos drills. | [ ] | QA |
| P2.7 | Behavioral | Switch ingest/chat to Haystack `AsyncPipeline`. | [ ] | Dev |
| P2.8 | Closure | Sync docs + record lessons in `00_journal.md`. | [ ] | Master |
| P2.9 | Stability | Orphan MinIO sweeper: TTL 24h on staging objects (`event=minio.orphan_object` audit). | [ ] | SRE |

## Phase 3 — Graph Enhancement (conditional, +4–6 weeks) — *gated*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P3.1 | Decision | ADR: Graph DB selection (Neo4j Community / ArcadeDB / Memgraph). | [ ] | Architect |
| P3.2 | Behavioral | Replace `StubGraphExtractor` with real `GraphExtractor` (same Protocol). | [ ] | Dev |
| P3.3 | Behavioral | `HybridRetrieverWithGraph` SuperComponent + `LightRAGRetriever` (200 ms TO → []). | [ ] | Dev |
| P3.4 | Governance | Entity soft-delete + ref_count + GC + reconciliation cron. | [ ] | Dev |
| P3.5 | Gate | P2 stable ≥ 4 weeks AND hybrid alone underperforms on relational queries. | [ ] | PM |
