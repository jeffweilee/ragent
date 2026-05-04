# 00_plan.md — Master TDD Implementation Checklist

> Source: `docs/00_spec.md` · Authored: 2026-05-03 · Revised: 2026-05-04
> Workflow: `CLAUDE.md` §THE TDD WORKFLOW · Tidy First (`[STRUCTURAL]` / `[BEHAVIORAL]`)
> Each `[ ]` = one Red→Green→Refactor cycle, each cycle = one (or two: structural-then-behavioral) commit.
> Revision driver: `docs/team/2026_05_04_phase1_review.md` — added ingest CRUD lifecycle (W2.5), pluggable pipeline registry (3.0), 3rd-party API clients (W3+), and OpenFGA dual-filter (W5).

---

## Phase 1 — Core MLP (5–7 weeks)

### W1 — Analysis & Skeleton

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 1.1 | Analysis | Define Domain Boundaries & Mission Objectives in `00_spec.md`. | [x] | Architect |
| 1.2 | Design | Map Business Scenarios & write Given-When-Then in `00_spec.md` §5. | [x] | QA / PM |
| 1.3 | Structural | Scaffold project: `pyproject.toml` (Python 3.12, uv, ruff, pytest), `src/ragent/`, `tests/{unit,integration,e2e}`. | [x] | Dev |
| 1.4 | Structural | CI command alias `make check` = `ruff format . && ruff check . --fix && pytest`. | [ ] | Dev |

### W2 — Plugin Protocol v1 (Red → Green → Refactor)

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 2.1 | Red | Plugin Protocol attribute/method conformance test. | [x] | QA |
| 2.2 | Green | `src/ragent/plugins/protocol.py` (`runtime_checkable` Protocol). | [x] | Dev |
| 2.3 | Red | Stub graph extractor no-op test. | [x] | QA |
| 2.4 | Green | `src/ragent/plugins/stub_graph.py`. | [x] | Dev |
| 2.5 | Refactor | Reviewed: no shared boilerplate; kept duplicated per YAGNI. | [x] | Reviewer |

### W2.5 — Ingest CRUD Lifecycle Foundations (NEW — team review 2026-05-04)

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 2.6 | Red | `tests/unit/test_id_gen.py` — `new_id()` returns 26-char Crockford base32; sortable across calls. | [ ] | QA |
| 2.7 | Green | `src/ragent/utility/id_gen.py` (UUIDv7 → 16 bytes → base32, ≤ 30 LOC). | [ ] | Dev |
| 2.8 | Red | `tests/unit/test_datetime_utility.py` — `utcnow()` always tz-aware UTC; `to_iso` ends in `Z`; `from_db` attaches UTC. | [ ] | QA |
| 2.9 | Green | `src/ragent/utility/datetime.py`. | [ ] | Dev |
| 2.10 | Red | `tests/unit/test_state_machine.py` — `update_status()` accepts UPLOADED→PENDING, PENDING→READY, PENDING→FAILED; rejects UPLOADED→FAILED, READY→PENDING, FAILED→READY (S10). | [ ] | QA |

### W3 — Ingest Repositories, Storage, and Service

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 3.0 | Red | `tests/unit/test_plugin_registry.py` — register + fan_out + all_required_ok; duplicate name raises (S11); required-failure → all_required_ok=False. | [ ] | QA |
| 3.0g | Green | `src/ragent/plugins/registry.py` (`PluginRegistry`, `Result`, `DuplicatePluginError`). | [ ] | Dev |
| 3.1 | Red | `tests/unit/test_vector_extractor.py` — already implemented. | [x] | QA |
| 3.2 | Green | `src/ragent/plugins/vector.py` — already implemented. | [x] | Dev |
| 3.3 | Red | `tests/unit/test_document_repository.py` — `create / get / acquire (FOR UPDATE) / update_status (state-machine guarded) / list_pending`. Uses sqlite-in-memory or fake. | [ ] | QA |
| 3.4 | Green | `src/ragent/repositories/document_repository.py` (Repository layer; only CRUD, no business logic). | [ ] | Dev |
| 3.5 | Red | `tests/unit/test_chunk_repository.py` — `bulk_insert / delete_by_document_id`. | [ ] | QA |
| 3.5g | Green | `src/ragent/repositories/chunk_repository.py`. | [ ] | Dev |
| 3.6 | Red | `tests/unit/test_minio_client.py` — `put_object` returns `minio://...` URI; `delete_object` idempotent. | [ ] | QA |
| 3.6g | Green | `src/ragent/storage/minio_client.py`. | [ ] | Dev |
| 3.7 | Red | `tests/unit/test_ingest_service.py` — orchestrates put → repo.create → kiq dispatch; rolls back row if MinIO put fails. | [ ] | QA |
| 3.7g | Green | `src/ragent/services/ingest_service.py` (Service layer, ≤ 30 LOC/method). | [ ] | Dev |
| 3.8 | Red | `tests/integration/test_ingest_pipeline.py` — Haystack ingest pipeline happy path (Convert→Clean→Lang→Split→Embed). Mock embedder. | [ ] | QA |
| 3.8g | Green | `src/ragent/pipelines/factory.py` + `pipelines/ingest.py`. | [ ] | Dev |

### W4 — Chat Pipeline + LLM/Embedding Clients + Token Manager

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 4.0 | Red | `tests/unit/test_token_manager.py` — refresh at `expiresAt − 5min` boundary using fake clock (S9). | [ ] | QA |
| 4.0g | Green | `src/ragent/clients/auth.py` (`TokenManager`). | [ ] | Dev |
| 4.1 | Red | `tests/unit/test_embedding_client.py` — POST shape, `bge-m3`, validates `returnCode == 96200`, retry 3× @ 1s. | [ ] | QA |
| 4.1g | Green | `src/ragent/clients/embedding.py`. | [ ] | Dev |
| 4.2 | Red | `tests/unit/test_llm_client.py` — streaming async iterator yields deltas; timeout 120s; retry 3× @ 2s. | [ ] | QA |
| 4.2g | Green | `src/ragent/clients/llm.py`. | [ ] | Dev |
| 4.3 | Red | `tests/unit/test_rerank_client.py` — POST shape, `bge-reranker-base`, `top_k=2`. (Wired in P2.) | [ ] | QA |
| 4.3g | Green | `src/ragent/clients/rerank.py`. | [ ] | Dev |
| 4.4 | Red | `tests/integration/test_chat_pipeline.py` — emits ≥1 `delta` then exactly one `done` with sources (S6). | [ ] | QA |
| 4.4g | Green | `src/ragent/pipelines/chat.py` (QueryEmbedder → {ESVector ∥ ESBM25} → DocumentJoiner(RRF) → LLM stream). | [ ] | Dev |

### W5 — Auth Layer + OpenFGA Dual-Filter

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 5.1 | Red | `tests/unit/test_jwt.py` — invalid → 401 on `/chat` and `/ingest`. | [ ] | QA |
| 5.1g | Green | `src/ragent/auth/jwt.py` (FastAPI dependency). | [ ] | Dev |
| 5.2 | Red | `tests/unit/test_openfga_client.py` — `list_resource` and `check` request/response per `00_rule.md`. | [ ] | QA |
| 5.2g | Green | `src/ragent/clients/openfga.py`. | [ ] | Dev |
| 5.3 | Red | `tests/unit/test_acl_filter.py` — `list_resource` result becomes ES `terms` filter on `document_id`. | [ ] | QA |
| 5.3g | Green | `src/ragent/auth/acl.py::build_es_filter(user_id)`. | [ ] | Dev |
| 5.4 | Red | `tests/integration/test_post_filter.py` — even if ES returns leaked doc, `check` drops + audit log (S7). | [ ] | QA |
| 5.4g | Green | Post-filter wired into `pipelines/chat.py`. | [ ] | Dev |
| 5.5 | Red | `tests/integration/test_user_isolation.py` — user A cannot retrieve user B's private doc. | [ ] | QA |

### W6 — Reconciler, MCP Schema, Observability, Acceptance

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 6.1 | Red | `tests/integration/test_reconciler_redispatch.py` — PENDING > 5 min → re-kiq, idempotent (S2). | [ ] | QA |
| 6.1g | Green | `src/ragent/reconciler.py` (TaskIQ scheduled, `SELECT … FOR UPDATE SKIP LOCKED`). | [ ] | Dev |
| 6.2 | Red | `tests/integration/test_reconciler_failed.py` — attempt > 5 → status=FAILED + structured-log alert (S3). | [ ] | QA |
| 6.2g | Green | Status transition + structured log line `event=ingest.failed`. | [ ] | Dev |
| 6.3 | Structural | OpenAPI schema for `POST /mcp/tools/rag` published; handler returns 501. | [ ] | Dev |
| 6.4 | Red | `tests/unit/test_mcp_endpoint.py` — returns 501 in P1 (S8). | [ ] | QA |
| 6.5 | Refactor | Wire OTEL: Haystack auto-trace + FastAPI middleware (no custom spans). | [ ] | SRE |
| 6.6 | Acceptance | E2E 100-doc ingest → success rate ≥ 99% (`tests/e2e/test_ingest_success_rate.py`). | [ ] | QA |
| 6.7 | Acceptance | Golden 50-Q top-3 ≥ 70% (`tests/e2e/test_golden_set.py`). | [ ] | QA |
| 6.8 | Acceptance | Chaos: kill worker mid-ingest → Reconciler recovers ≤ 10 min. | [ ] | SRE |

---

## Phase 2 — Production Quality (+3 weeks) — *not started*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 7.1 | Stability | SRE: HA verification, monitoring, alerting rules. | [ ] | SRE |
| 7.2 | Behavioral | Wire `RerankClient` into chat pipeline as `HybridRetrieverWithRerank` SuperComponent. | [ ] | Dev |
| 7.3 | Behavioral | `ConditionalRouter` intent split (translate/summarize → Direct LLM). | [ ] | Dev |
| 7.4 | Behavioral | MCP Tool real handler. | [ ] | Dev |
| 7.5 | Behavioral | `HRClient` + JWT-subject → employee resolution; populate `documents.owner_user_id` from HR. | [ ] | Dev |
| 7.6 | Quality | RAGAS eval in CI; large-file streaming; chaos drills. | [ ] | QA |
| 7.7 | Behavioral | Switch ingest/chat to Haystack `AsyncPipeline`. | [ ] | Dev |
| 7.8 | Closure | Sync docs + record lessons in `00_journal.md`. | [ ] | Master |

## Phase 3 — Graph Enhancement (conditional, +4–6 weeks) — *gated*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 8.1 | Decision | ADR: Graph DB selection (Neo4j Community / ArcadeDB / Memgraph). | [ ] | Architect |
| 8.2 | Behavioral | Replace `StubGraphExtractor` with real `GraphExtractor` (same Protocol). | [ ] | Dev |
| 8.3 | Behavioral | `HybridRetrieverWithGraph` SuperComponent + `LightRAGRetriever` (200 ms TO → []). | [ ] | Dev |
| 8.4 | Governance | Entity soft-delete + ref_count + GC + reconciliation cron. | [ ] | Dev |
| 8.5 | Gate | P2 stable ≥ 4 weeks AND hybrid alone underperforms on relational queries. | [ ] | PM |

---

## Definition of Done — Phase 1

- [ ] All Phase 1 boxes ticked `[x]`.
- [ ] `uv run ruff format . && uv run ruff check . && uv run pytest` exits 0.
- [ ] Exit metrics 6.6 / 6.7 / 6.8 met.
- [ ] `00_journal.md` updated with at least one Phase 1 lesson per domain encountered.
- [ ] Every BDD scenario in `00_spec.md` §5 has a corresponding plan row whose test path matches the scenario name.
