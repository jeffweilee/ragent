# 00_plan.md — Master TDD Implementation Checklist

> Source: `docs/00_spec.md` · Authored: 2026-05-03
> Workflow: `CLAUDE.md` §THE TDD WORKFLOW · Tidy First (`[STRUCTURAL]` / `[BEHAVIORAL]`)
> Each `[ ]` = one Red→Green→Refactor cycle, each cycle = one (or two: structural-then-behavioral) commit.

---

## Phase 1 — Core MLP (5–7 weeks)

### W1 — Analysis & Skeleton

| # | Category | Task | Status | Owner | Cycle |
|---|---|---|:---:|---|---|
| 1.1 | Analysis | Define Domain Boundaries & Mission Objectives in `00_spec.md`. | [x] | Architect | n/a |
| 1.2 | Design | Map Business Scenarios & write Given-When-Then in `00_spec.md` §5. | [x] | QA / PM | n/a |
| 1.3 | Structural | Scaffold project: `pyproject.toml` (Python 3.12, uv, ruff, pytest), `src/ragent/`, `tests/{unit,integration,e2e}`. | [x] | Dev | STRUCT |
| 1.4 | Structural | CI command alias `make check` = `ruff format . && ruff check . --fix && pytest`. | [ ] | Dev | STRUCT |

### W2 — Plugin Protocol v1 (Red → Green → Refactor)

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 2.1 | Red | `tests/unit/test_plugin_protocol.py::test_protocol_attributes_required` — assert any class missing `name/required/queue/extract/delete/health` fails `isinstance(x, ExtractorPlugin)`. | [x] | QA |
| 2.2 | Green | Implement `src/ragent/plugins/protocol.py` with `runtime_checkable` `Protocol`. | [x] | Dev |
| 2.3 | Red | `test_stub_graph_extractor_is_noop` — `extract` returns None, `health` True, no side effects. | [x] | QA |
| 2.4 | Green | Implement `src/ragent/plugins/stub_graph.py`. | [x] | Dev |
| 2.5 | Refactor | Extract `BaseExtractor` if `VectorExtractor` shares boilerplate; otherwise leave duplicated (YAGNI). | [ ] | Reviewer |

### W3 — Vector Extractor + Ingest Pipeline

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 3.1 | Red | `test_vector_extractor_batch_indexes_to_es` — given chunks, calls embedder once, ES `bulk` once, idempotent on rerun. | [ ] | QA |
| 3.2 | Green | Implement `src/ragent/plugins/vector.py` (mock embedder + ES client at unit level). | [ ] | Dev |
| 3.3 | Red | `test_ingest_pipeline_happy_path` (integration) — Converter→Cleaner→LanguageRouter→Splitter→Embedder→write chunks. | [ ] | QA |
| 3.4 | Green | Implement Haystack ingest pipeline assembly in `src/ragent/pipelines/ingest.py`. | [ ] | Dev |
| 3.5 | Refactor | Pull pipeline factory into `pipelines/factory.py` if reused by tests. | [ ] | Reviewer |

### W4 — Chat Pipeline (Hybrid Retrieval)

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 4.1 | Red | `test_chat_pipeline_emits_sse_delta_then_done` — at least one `delta`, exactly one `done` with `sources`. | [ ] | QA |
| 4.2 | Green | Implement `pipelines/chat.py`: QueryEmbedder → {ESVectorRetriever ∥ ESBM25Retriever} → DocumentJoiner(RRF) → LLM stream. | [ ] | Dev |
| 4.3 | Red | `test_rrf_join_combines_results` — given two ranked lists, output reflects RRF order. | [ ] | QA |
| 4.4 | Green | Use Haystack `DocumentJoiner(join_mode="reciprocal_rank_fusion")`. | [ ] | Dev |
| 4.5 | Refactor | Extract LLM streaming wrapper if duplicated with future MCP path. | [ ] | Reviewer |

### W5 — Auth & Permission Dual-Layer

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 5.1 | Red | `test_jwt_invalid_returns_401` on `/chat` & `/ingest`. | [ ] | QA |
| 5.2 | Green | FastAPI dependency `verify_jwt` in `src/ragent/auth/jwt.py`. | [ ] | Dev |
| 5.3 | Red | `test_es_filter_excludes_unauthorized_docs` — query injects `acl_user_ids` term filter. | [ ] | QA |
| 5.4 | Green | Implement `auth/acl.py::build_es_filter(user_id)`. | [ ] | Dev |
| 5.5 | Red | `test_post_filter_drops_leaked_doc` — even if ES returns extra, post-filter drops + audit logs. | [ ] | QA |
| 5.6 | Green | Implement post-filter in `pipelines/chat.py`. | [ ] | Dev |
| 5.7 | Red | `test_user_a_cannot_see_user_b_private` (integration) — covers spec S7. | [ ] | QA |

### W6 — Resilience, MCP Schema, Exit Verification

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 6.1 | Red | `test_reconciler_redispatches_pending_after_5min` (integration with fake clock). | [ ] | QA |
| 6.2 | Green | Implement `src/ragent/reconciler.py` (TaskIQ scheduled, idempotent by `(doc_id, attempt)`). | [ ] | Dev |
| 6.3 | Red | `test_reconciler_marks_failed_after_5_attempts` + alert log assertion. | [ ] | QA |
| 6.4 | Green | Status transition + structured log line `event=ingest.failed doc_id=… attempt=6`. | [ ] | Dev |
| 6.5 | Structural | OpenAPI schema for `POST /mcp/tools/rag` published; handler returns 501. | [ ] | Dev |
| 6.6 | Red | `test_mcp_rag_returns_501_in_phase_1`. | [ ] | QA |
| 6.7 | Refactor | Wire OTEL: Haystack auto-trace + FastAPI middleware (no custom spans). | [ ] | SRE |
| 6.8 | Acceptance | E2E 100-doc ingest → success rate ≥ 99% (`tests/e2e/test_ingest_success_rate.py`). | [ ] | QA |
| 6.9 | Acceptance | Golden 50-Q top-3 ≥ 70% (`tests/e2e/test_golden_set.py`). | [ ] | QA |
| 6.10 | Acceptance | Chaos: kill worker mid-ingest → Reconciler recovers ≤ 10 min. | [ ] | SRE |

---

## Phase 2 — Production Quality (+3 weeks) — *not started*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 7.1 | Stability | SRE: HA verification, monitoring, alerting rules. | [ ] | SRE |
| 7.2 | Behavioral | Mount Rerank API SuperComponent (`HybridRetrieverWithRerank`). | [ ] | Dev |
| 7.3 | Behavioral | `ConditionalRouter` intent split (translate/summarize → Direct LLM). | [ ] | Dev |
| 7.4 | Behavioral | MCP Tool real handler. | [ ] | Dev |
| 7.5 | Quality | RAGAS eval in CI; large-file streaming; chaos drills. | [ ] | QA |
| 7.6 | Behavioral | Switch ingest/chat to Haystack `AsyncPipeline`. | [ ] | Dev |
| 7.7 | Closure | Sync docs + record lessons in `00_journal.md`. | [ ] | Master |

## Phase 3 — Graph Enhancement (conditional, +4–6 weeks) — *gated*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| 8.1 | Decision | ADR: Graph DB selection (Neo4j Community / ArcadeDB / Memgraph). | [ ] | Architect |
| 8.2 | Behavioral | Replace `StubGraphExtractor` with real `GraphExtractor` (same Protocol). | [ ] | Dev |
| 8.3 | Behavioral | `HybridRetrieverWithGraph` SuperComponent + `LightRAGRetriever` (200 ms TO → []). | [ ] | Dev |
| 8.4 | Governance | Entity soft-delete + ref_count + GC + reconciliation cron. | [ ] | Dev |
| 8.5 | Gate | Verify P2 stable ≥ 4 weeks AND hybrid alone underperforms on relational queries. | [ ] | PM |

---

## Definition of Done — Phase 1

- [ ] All Phase 1 boxes above ticked `[x]`.
- [ ] `uv run ruff format . && uv run ruff check . && uv run pytest` exits 0.
- [ ] Exit metrics 6.8 / 6.9 / 6.10 met.
- [ ] `00_journal.md` updated with at least one Phase 1 lesson per domain encountered.
