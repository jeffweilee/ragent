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
| T0.2 | Structural | CI alias `make check` = `ruff format . && ruff check . --fix && pytest`. | T0.1 | [ ] | Dev | W1 |
| T0.3 | Red | `tests/unit/test_id_gen.py` — `new_id()` returns 26-char Crockford base32; sortable across calls. | T0.1 | [ ] | QA | W2 |
| T0.4 | Green | `src/ragent/utility/id_gen.py` (UUIDv7 → 16 bytes → base32; ≤ 30 LOC). | T0.3 | [ ] | Dev | W2 |
| T0.5 | Red | `tests/unit/test_datetime_utility.py` — `utcnow()` tz-aware UTC; `to_iso` ends in `Z`; `from_db` attaches UTC. | T0.1 | [ ] | QA | W2 |
| T0.6 | Green | `src/ragent/utility/datetime.py`. | T0.5 | [ ] | Dev | W2 |
| T0.7 | Red | `tests/unit/test_state_machine.py` — accepts `{UPLOADED→PENDING, PENDING→READY, PENDING→FAILED, PENDING→DELETING, READY→DELETING, FAILED→DELETING}`; rejects `{UPLOADED→FAILED, READY→PENDING, FAILED→READY, DELETING→READY}` (S10). | T0.1 | [ ] | QA | W2 |

### Track T1 — Plugins (Protocol + Registry + Extractors)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T1.1 | Red | `tests/unit/test_plugin_protocol.py` — protocol attribute/method conformance (S4). | T0.1 | [x] | QA | W2 |
| T1.2 | Green | `src/ragent/plugins/protocol.py` (`runtime_checkable` Protocol). | T1.1 | [x] | Dev | W2 |
| T1.3 | Red | Stub graph extractor no-op test (S5). | T0.1 | [x] | QA | W2 |
| T1.4 | Green | `src/ragent/plugins/stub_graph.py`. | T1.3 | [x] | Dev | W2 |
| T1.5 | Refactor | Reviewed: no shared boilerplate; kept duplicated per YAGNI. | T1.4 | [x] | Reviewer | W2 |
| T1.6 | Red | `tests/unit/test_plugin_registry.py` — register, fan_out, all_required_ok; duplicate name raises (S11). | T1.2 | [ ] | QA | W3 |
| T1.7 | Green | `src/ragent/plugins/registry.py` (`PluginRegistry`, `Result`, `DuplicatePluginError`). | T1.6 | [ ] | Dev | W3 |
| T1.8 | Red | `tests/unit/test_plugin_registry_delete.py` — `fan_out_delete` calls every registered plugin; idempotent on already-deleted. | T1.7 | [ ] | QA | W3 |
| T1.9 | Red | `tests/unit/test_vector_extractor.py` — Protocol conformance, embedder/ES bulk once, idempotent rerun, delete clears chunks. | T1.2 | [x] | QA | W3 |
| T1.10 | Green | `src/ragent/plugins/vector.py`. | T1.9 | [x] | Dev | W3 |

### Track T2 — Ingest CRUD (Repositories + Storage + Service + Router)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T2.1 | Red  | `tests/unit/test_document_repository.py` — `create (with optional source_id) / get / acquire (FOR UPDATE) / update_status (state-machine guarded) / list_pending / list / delete / list_ready_by_source(source_id) FOR UPDATE SKIP LOCKED` (returns all READY rows; service picks MAX(created_at) survivor). | T0.4, T0.6, T0.7 | [ ] | QA | W3 |
| T2.2 | Green | `src/ragent/repositories/document_repository.py` (Repository layer; CRUD only). | T2.1 | [ ] | Dev | W3 |
| T2.3 | Red  | `tests/unit/test_chunk_repository.py` — `bulk_insert / delete_by_document_id`. | T0.4 | [ ] | QA | W3 |
| T2.4 | Green | `src/ragent/repositories/chunk_repository.py`. | T2.3 | [ ] | Dev | W3 |
| T2.5 | Red  | `tests/unit/test_minio_client.py` — `put_object` returns `minio://...`; `delete_object` idempotent. MinIO is transient staging only — cleared on terminal pipeline state. | T0.1 | [ ] | QA | W3 |
| T2.6 | Green | `src/ragent/storage/minio_client.py`. | T2.5 | [ ] | Dev | W3 |
| T2.7 | Red  | `tests/unit/test_ingest_service_create.py` — MIME validate (≤50 MB, allow-list per spec §4.2) → put → repo.create (persists optional `source_id`) → kiq dispatch; rolls back row if MinIO put fails. | T2.2, T2.6, T1.7 | [ ] | QA | W3 |
| T2.8 | Green | `src/ragent/services/ingest_service.py::create` (≤ 30 LOC/method). | T2.7 | [ ] | Dev | W3 |
| T2.9 | Red  | `tests/unit/test_ingest_service_delete.py` — cascade order (acquire→DELETING → fan_out_delete → chunks → row; MinIO already cleared at terminal state, deleted in cascade only if status is UPLOADED/PENDING); on any failure row stays DELETING (S13); idempotent re-delete returns 204 (S14). | T2.8, T1.8 | [ ] | QA | W3 |
| T2.10 | Green | `src/ragent/services/ingest_service.py::delete`. | T2.9 | [ ] | Dev | W3 |
| T2.11 | Red | `tests/unit/test_ingest_service_list.py` — cursor pagination by `document_id` ASC; `next_cursor` correctness (S15, P1 OPEN: no ACL filter). | T2.2 | [ ] | QA | W3 |
| T2.12 | Green | `src/ragent/services/ingest_service.py::list`. | T2.11 | [ ] | Dev | W3 |
| T2.13 | Red | `tests/unit/test_ingest_router.py` — Router only parses/validates and delegates; 415 on bad MIME, 413 on >50 MB; `X-User-Id` required (P1 OPEN). | T2.8, T2.10, T2.12 | [ ] | QA | W3 |
| T2.14 | Green | `src/ragent/routers/ingest.py` (declares all endpoints in spec §4.1). | T2.13 | [ ] | Dev | W3 |

### Track T3 — Pipelines (Ingest + Chat assembly)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T3.1 | Red | `tests/integration/test_ingest_pipeline.py` — Haystack Convert→Clean→Lang→Split→Embed; mock embedder. | T2.4, T4.2 | [ ] | QA | W3 |
| T3.2 | Green | `src/ragent/pipelines/factory.py` + `pipelines/ingest.py`. | T3.1 | [ ] | Dev | W3 |
| T3.2a | Red | `tests/integration/test_worker_minio_cleanup.py` — terminal status (`READY` or `FAILED`) is committed **before** `MinIOClient.delete_object` is called; if the delete raises, the document still ends up in the terminal status and an `event=minio.orphan_object` log is emitted (S16, S21). On retry path (still `PENDING`), MinIO object is retained. | T3.2 | [ ] | QA | W3 |
| T3.2b | Green | Worker task: commit terminal status first; then best-effort `MinIOClient.delete_object` (errors logged, not raised). | T3.2a | [ ] | Dev | W3 |
| T3.2c | Red | `tests/integration/test_supersede_task.py` — on `READY` with `source_id` set, kiq `ingest.supersede`; task selects all `READY` rows with same `source_id`, keeps `MAX(created_at)`, cascade-deletes the rest (S17); out-of-order finish still converges to MAX(created_at) survivor (S20); FAILED never enqueues supersede (S18); idempotent re-run (S19). | T3.2b, T2.10 | [ ] | QA | W3 |
| T3.2d | Green | `src/ragent/services/ingest_service.py::supersede(document_id)` + TaskIQ `ingest.supersede` worker; selects READY peers, keeps MAX(created_at), reuses cascade delete path for losers. | T3.2c | [ ] | Dev | W3 |
| T3.3 | Red | `tests/integration/test_chat_pipeline.py` — emits ≥1 `delta` then exactly one `done` with sources (S6); P1 OPEN: ACL filter no-op. | T4.4, T4.6 | [ ] | QA | W4 |
| T3.4 | Green | `src/ragent/pipelines/chat.py` (QueryEmbedder → {ESVector ∥ ESBM25} → DocumentJoiner(RRF) → LLM stream). | T3.3 | [ ] | Dev | W4 |

### Track T4 — Third-Party Clients

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T4.1 | Red | `tests/unit/test_token_manager.py` — refresh at `expiresAt − 5min` boundary using fake clock (S9). | T0.6 | [ ] | QA | W4 |
| T4.2 | Green | `src/ragent/clients/auth.py` (`TokenManager`). | T4.1 | [ ] | Dev | W4 |
| T4.3 | Red | `tests/unit/test_embedding_client.py` — POST shape, `bge-m3`, validates `returnCode == 96200`, retry 3× @ 1 s. | T4.2 | [ ] | QA | W4 |
| T4.4 | Green | `src/ragent/clients/embedding.py`. | T4.3 | [ ] | Dev | W4 |
| T4.5 | Red | `tests/unit/test_llm_client.py` — streaming async iterator yields deltas; timeout 120 s; retry 3× @ 2 s. | T4.2 | [ ] | QA | W4 |
| T4.6 | Green | `src/ragent/clients/llm.py`. | T4.5 | [ ] | Dev | W4 |
| T4.7 | Red | `tests/unit/test_rerank_client.py` — POST shape, `bge-reranker-base`, `top_k=2`. (Wired P2.) | T4.2 | [ ] | QA | W4 |
| T4.8 | Green | `src/ragent/clients/rerank.py`. | T4.7 | [ ] | Dev | W4 |

### Track T5 — Resilience (Reconciler)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T5.1 | Red | `tests/integration/test_reconciler_redispatch.py` — PENDING > 5 min → re-kiq, idempotent (S2). | T2.2, T2.8 | [ ] | QA | W6 |
| T5.2 | Green | `src/ragent/reconciler.py` (TaskIQ scheduled, `SELECT … FOR UPDATE SKIP LOCKED`). | T5.1 | [ ] | Dev | W6 |
| T5.3 | Red | `tests/integration/test_reconciler_failed.py` — attempt > 5 → status=FAILED + structured-log alert (S3). | T5.2 | [ ] | QA | W6 |
| T5.4 | Green | Status transition + structured log line `event=ingest.failed`. | T5.3 | [ ] | Dev | W6 |
| T5.5 | Red | `tests/integration/test_reconciler_delete_resume.py` — DELETING > 5 min → resume cascade idempotently (S13). | T2.10 | [ ] | QA | W6 |
| T5.6 | Green | Reconciler resumes DELETING. | T5.5 | [ ] | Dev | W6 |

### Track T6 — MCP Schema (501 in P1)

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T6.1 | Structural | OpenAPI schema for `POST /mcp/tools/rag` published; handler returns 501. | T2.14 | [ ] | Dev | W6 |
| T6.2 | Red | `tests/unit/test_mcp_endpoint.py` — returns 501 in P1 (S8). | T6.1 | [ ] | QA | W6 |

### Track T7 — Observability + Acceptance

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T7.1 | Refactor | Wire OTEL: Haystack auto-trace + FastAPI middleware (no custom spans). Logs include `auth_mode=open` field. | T3.4 | [ ] | SRE | W6 |
| T7.2 | Acceptance | E2E 100-doc ingest → success rate ≥ 99% (`tests/e2e/test_ingest_success_rate.py`). | T3.2, T5.6 | [ ] | QA | W6 |
| T7.3 | Acceptance | Golden 50-Q top-3 ≥ 70% (`tests/e2e/test_golden_set.py`). | T3.4 | [ ] | QA | W6 |
| T7.4 | Acceptance | Chaos: kill worker mid-ingest → Reconciler recovers ≤ 10 min. | T5.6 | [ ] | SRE | W6 |
| T7.5 | Structural | Startup guard: `bootstrap.py` refuses to start unless `RAGENT_AUTH_DISABLED=true AND RAGENT_ENV=dev`; bind 127.0.0.1 only in dev. | T2.14 | [ ] | SRE | W6 |
| T7.6 | Red | `tests/unit/test_bootstrap_startup_guard.py` — non-dev env or AUTH_DISABLED unset → SystemExit. | T7.5 | [ ] | QA | W6 |

### Track T8 — Auth & Permission `[~] DISABLED IN P1 → P2`

> P1 produces NO code in this track. **OpenFGA is out-of-scope across all phases.** Interface in `00_spec.md` §3.5; implementation lands in P2.

| # | Category | Task | Depends On | Status | Owner | Phase |
|---|---|---|---|:---:|---|:---:|
| T8.1 | Red  | `tests/unit/test_jwt.py` — invalid → 401 on `/chat` and `/ingest`. | (P2 entry) | [~] | QA | P2 |
| T8.2 | Green | `src/ragent/auth/jwt.py` (FastAPI dependency). | T8.1 | [~] | Dev | P2 |
| T8.3 | Red  | `tests/unit/test_owner_acl_filter.py` — `build_es_filter(user_id)` produces `terms(owner_user_id ∈ [user_id])`. | T8.2 | [~] | QA | P2 |
| T8.4 | Green | `src/ragent/auth/acl.py::build_es_filter(user_id)` (owner-based, no external service). | T8.3 | [~] | Dev | P2 |
| T8.5 | Red  | `tests/integration/test_user_isolation.py` — user A cannot retrieve user B's documents via `/chat` or `/ingest`. | T8.4 | [~] | QA | P2 |
| T8.6 | Green | Wire owner-based pre-filter into `pipelines/chat.py` and `services/ingest_service.py::list/get/delete`. | T8.5 | [~] | Dev | P2 |
| T8.7 | Behavioral | `HRClient` + JWT-subject → employee resolution; populate `documents.owner_user_id`. | T8.2 | [~] | Dev | P2 |

---

## Definition of Done — Phase 1

- [ ] Every `[ ]` row in tracks T0–T7 is `[x]`.
- [ ] T8 rows remain `[~]` (auth disabled by design); spec §3.5 and §4.5 still describe the P2 contract.
- [ ] `uv run ruff format . && uv run ruff check . && uv run pytest` exits 0.
- [ ] Acceptance metrics T7.2 / T7.3 / T7.4 met.
- [ ] Startup guard (T7.5) verified (`pytest tests/unit/test_bootstrap_startup_guard.py`).
- [ ] Every BDD scenario in `00_spec.md` §3.X has a corresponding plan row whose test path matches.
- [ ] `00_journal.md` carries at least one P1 lesson per domain encountered.

---

## Phase 2 — Production Quality (+3 weeks) — *not started*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P2.1 | Stability | SRE: HA verification, monitoring, alerting rules. | [ ] | SRE |
| P2.2 | Security | Re-enable JWT auth + owner-based ACL per Track T8 (all `[~]` rows → `[ ]` → `[x]`). Remove `RAGENT_AUTH_DISABLED` env knob. | [ ] | Dev |
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
