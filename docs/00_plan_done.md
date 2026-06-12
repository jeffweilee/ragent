# 00_plan_done.md — Completed & Descoped Tracks Archive

> Tracks move here **in full** only when every item is `[x]` or `[~]`.
> Active incomplete tracks live in [`docs/00_plan.md`](00_plan.md).
> Ordered chronologically by track completion date.

## Status legend
- `[x]` delivered
- `[~]` descoped / deferred

---

## Phase 1 — Foundation Tracks (T0–T8)

> Closed 2026-05-10 against `origin/main@42781a3`.

---

### Track T0 — Foundations (utilities & state machine)

**Counter: 完成 20 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T0.1 | Structural | • **Achieve:** Scaffold the initial project tree.<br>• **Deliver:** `pyproject.toml`, `src/ragent/`, `tests/{unit,integration,e2e}/`. | — | [x] | Dev | W1 |
| T0.2 | Structural | • **Achieve:** Lock in CI quality gate with coverage floor (DoD).<br>• **Deliver:** `make check` alias = `ruff format . && ruff check . --fix && pytest --cov=src/ragent --cov-branch --cov-fail-under=92`; CI fails on coverage drop. | T0.1 | [x] | Dev | W1 |
| T0.3 | Red | • **Achieve:** Pin sortable, URL-safe ID contract.<br>• **Deliver:** `tests/unit/test_id_gen.py` — `new_id()` returns 26-char Crockford base32; sortable across calls. | T0.1 | [x] | QA | W2 |
| T0.4 | Green | • **Achieve:** Implement UUIDv7-based ID generator.<br>• **Deliver:** `src/ragent/utility/id_gen.py` (UUIDv7 → 16 bytes → base32; ≤ 30 LOC). | T0.3 | [x] | Dev | W2 |
| T0.5 | Red | • **Achieve:** Pin end-to-end UTC datetime contract.<br>• **Deliver:** `tests/unit/test_datetime_utility.py` — `utcnow()` tz-aware UTC; `to_iso` ends in `Z`; `from_db` attaches UTC. | T0.1 | [x] | QA | W2 |
| T0.6 | Green | • **Achieve:** Implement UTC datetime helpers.<br>• **Deliver:** `src/ragent/utility/datetime.py`. | T0.5 | [x] | Dev | W2 |
| T0.7 | Red | • **Achieve:** Pin document state-machine transitions (S10).<br>• **Deliver:** `tests/unit/test_state_machine.py`. | T0.1 | [x] | QA | W2 |
| T0.8 | Structural | • **Achieve:** Establish persistent schema for documents + chunks (B3).<br>• **Deliver:** `migrations/001_initial.sql`. | T0.1 | [x] | Dev | W2 |
| T0.8a | Structural | • **Achieve:** Keep a head-of-tree schema snapshot for drift detection (B3).<br>• **Deliver:** `migrations/schema.sql`. | T0.8 | [x] | Dev | W2 |
| T0.8e | Structural | • **Achieve:** Check in canonical ES index definition (B26).<br>• **Deliver:** `resources/es/chunks_v1.json`. | T0.1 | [x] | Dev | W2 |
| T0.9 | Structural | • **Achieve:** Provide reusable testcontainer fixtures for all integration tests (B8).<br>• **Deliver:** `tests/conftest.py` — session-scoped fixtures for MariaDB / ES / Redis / MinIO. | T0.1 | [x] | Dev | W2 |
| T0.8b | Red | • **Achieve:** Guarantee `schema.sql` ≡ `alembic upgrade head` (B3 invariant).<br>• **Deliver:** `tests/integration/test_schema_drift.py`. | T0.8a, T0.9 | [x] | QA | W2 |
| T0.8f | Red | • **Achieve:** Prevent prose/resource drift on ES index (B26).<br>• **Deliver:** `tests/integration/test_es_resource_drift.py`. | T0.8e | [x] | QA | W2 |
| T0.8c | Red | • **Achieve:** Verify idempotent first-boot auto-init across MariaDB + ES (B3, B4).<br>• **Deliver:** `tests/integration/test_bootstrap_auto_init.py`. | T0.8a, T0.8e, T0.9 | [x] | QA | W2 |
| T0.8d | Green | • **Achieve:** Implement non-destructive schema bootstrap.<br>• **Deliver:** `src/ragent/bootstrap/init_schema.py`. | T0.8c | [x] | Dev | W2 |
| T0.8g | Red | • **Achieve:** Fail closed when ES `analysis-icu` plugin is missing (B26, I5).<br>• **Deliver:** `tests/integration/test_es_plugin_required.py`. | T0.8d, T0.9 | [x] | QA | W2 |
| T0.10 | Structural | • **Achieve:** Provide one canonical TaskIQ broker dispatching on Redis topology (B27/B28).<br>• **Deliver:** `src/ragent/bootstrap/broker.py`. | T0.1 | [x] | Dev | W2 |
| T0.10a | Red | • **Achieve:** Pin broker topology dispatch behavior (B27).<br>• **Deliver:** `tests/unit/test_broker_topology.py`. | T0.10 | [x] | QA | W2 |
| T0.11 | Structural | • **Achieve:** Single source of truth for operator env config (B30).<br>• **Deliver:** `.env.example` enumerating every variable from spec §4.6. | T0.1 | [x] | Dev | W2 |
| T0.11a | Red | • **Achieve:** CI gate against operator-config drift (B30).<br>• **Deliver:** `tests/unit/test_env_example_drift.py`. | T0.11 | [x] | QA | W2 |

---

### Track T1 — Plugins (Protocol + Registry + Extractors)

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T1.1 | Red | • **Achieve:** Pin plugin Protocol shape (S4).<br>• **Deliver:** `tests/unit/test_plugin_protocol.py`. | T0.1 | [x] | QA | W2 |
| T1.2 | Green | • **Achieve:** Provide runtime-checkable plugin Protocol.<br>• **Deliver:** `src/ragent/plugins/protocol.py`. | T1.1 | [x] | Dev | W2 |
| T1.3 | Red | • **Achieve:** Pin stub graph extractor no-op behavior (S5).<br>• **Deliver:** Stub graph extractor no-op test. | T0.1 | [x] | QA | W2 |
| T1.4 | Green | • **Achieve:** Implement stub graph extractor placeholder for P1.<br>• **Deliver:** `src/ragent/plugins/stub_graph.py`. | T1.3 | [x] | Dev | W2 |
| T1.5 | Refactor | • **Achieve:** Confirm no premature abstraction across plugins (YAGNI).<br>• **Deliver:** Review note — no shared boilerplate; kept duplicated. | T1.4 | [x] | Reviewer | W2 |
| T1.6 | Red | • **Achieve:** Pin registry semantics: register, fan_out, timeout, dup-detect (S11, R6, S29).<br>• **Deliver:** `tests/unit/test_plugin_registry.py`. | T1.2 | [x] | QA | W3 |
| T1.7 | Green | • **Achieve:** Implement plugin registry with concurrent fan-out.<br>• **Deliver:** `src/ragent/plugins/registry.py`. | T1.6 | [x] | Dev | W3 |
| T1.8 | Red | • **Achieve:** Pin `fan_out_delete` semantics: idempotent, no DB tx held (R10, P-E).<br>• **Deliver:** `tests/unit/test_plugin_registry_delete.py`. | T1.7 | [x] | QA | W3 |
| T1.9 | Red | • **Achieve:** Pin VectorExtractor contract: idempotent ingest, clean delete.<br>• **Deliver:** `tests/unit/test_vector_extractor.py`. | T1.2 | [x] | QA | W3 |
| T1.10 | Green | • **Achieve:** Implement vector extractor plugin.<br>• **Deliver:** `src/ragent/plugins/vector.py`. | T1.9 | [x] | Dev | W3 |
| T1.11 | Red | • **Achieve:** Pin title-aware embedding + DI shape + ES doc fields (B15+B17+B29).<br>• **Deliver:** `tests/unit/test_vector_extractor_title.py`. | T1.10, T2.2, T0.8e | [x] | QA | W3+ |
| T1.12 | Green | • **Achieve:** Amend extractor for title-prefixed embedding + denormalised fields (B17, B29).<br>• **Deliver:** Updated `src/ragent/plugins/vector.py`. | T1.11 | [x] | Dev | W3+ |

---

### Track T2 — Ingest CRUD (Repositories + Storage + Service + Router)

**Counter: 完成 14 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T2.1 | Red | • **Achieve:** Pin DocumentRepository CRUD + lock + lifecycle queries (B11, B14, B16, B17, B25, B29, R1/R3/R7, S28/S33).<br>• **Deliver:** `tests/unit/test_document_repository.py`. | T0.4, T0.6, T0.7 | [x] | QA | W3 |
| T2.2 | Green | • **Achieve:** Implement document persistence layer (CRUD only).<br>• **Deliver:** `src/ragent/repositories/document_repository.py`. | T2.1 | [x] | Dev | W3 |
| T2.3 | Red | • **Achieve:** Pin chunk persistence contract.<br>• **Deliver:** `tests/unit/test_chunk_repository.py`. | T0.4 | [x] | QA | W3 |
| T2.4 | Green | • **Achieve:** Implement chunk persistence layer.<br>• **Deliver:** `src/ragent/repositories/chunk_repository.py`. | T2.3 | [x] | Dev | W3 |
| T2.5 | Red | • **Achieve:** Pin MinIO client object-key format, timeouts, and key-only return contract (B10, B25, C3).<br>• **Deliver:** `tests/unit/test_minio_client.py`. | T0.1 | [x] | QA | W3 |
| T2.6 | Green | • **Achieve:** Implement MinIO client adapter.<br>• **Deliver:** `src/ragent/storage/minio_client.py`. | T2.5 | [x] | Dev | W3 |
| T2.7 | Red | • **Achieve:** Pin ingest-create service contract (B11, B25, C1, S23).<br>• **Deliver:** `tests/unit/test_ingest_service_create.py`. | T2.2, T2.6, T1.7 | [x] | QA | W3 |
| T2.8 | Green | • **Achieve:** Implement ingest-create service path.<br>• **Deliver:** `src/ragent/services/ingest_service.py::create`. | T2.7 | [x] | Dev | W3 |
| T2.9 | Red | • **Achieve:** Pin delete cascade order, idempotency, no-tx-during-fan-out (P-E, S13/S14).<br>• **Deliver:** `tests/unit/test_ingest_service_delete.py`. | T2.8, T1.8 | [x] | QA | W3 |
| T2.10 | Green | • **Achieve:** Implement delete cascade.<br>• **Deliver:** `src/ragent/services/ingest_service.py::delete`. | T2.9 | [x] | Dev | W3 |
| T2.11 | Red | • **Achieve:** Pin cursor pagination + limit clamp (S15).<br>• **Deliver:** `tests/unit/test_ingest_service_list.py`. | T2.2 | [x] | QA | W3 |
| T2.12 | Green | • **Achieve:** Implement list service path.<br>• **Deliver:** `src/ragent/services/ingest_service.py::list`. | T2.11 | [x] | Dev | W3 |
| T2.13 | Red | • **Achieve:** Pin router as thin parse/validate/delegate layer with RFC 9457 errors (B5, B11, S23).<br>• **Deliver:** `tests/unit/test_ingest_router.py`. | T2.8, T2.10, T2.12 | [x] | QA | W3 |
| T2.14 | Green | • **Achieve:** Implement ingest router + RFC 9457 problem builder.<br>• **Deliver:** `src/ragent/routers/ingest.py` + `src/ragent/errors/problem.py`. | T2.13 | [x] | Dev | W3 |

---

### Track T3 — Pipelines (Ingest + Chat assembly)

**Counter: 完成 33 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T3.1 | Red | • **Achieve:** Pin language-routed ingest pipeline shape (B1).<br>• **Deliver:** `tests/integration/test_ingest_pipeline.py`. | T2.4, T4.2 | [x] | QA | W3 |
| T3.2 | Green | • **Achieve:** Implement ingest pipeline factory.<br>• **Deliver:** `src/ragent/pipelines/ingest.py`. | T3.1 | [x] | Dev | W3 |
| T3.2a | Red | • **Achieve:** Pin terminal-commit-before-MinIO-delete order (S16, S21).<br>• **Deliver:** `tests/integration/test_worker_minio_cleanup.py`. | T3.2 | [x] | QA | W3 |
| T3.2b | Green | • **Achieve:** Implement ingest worker task with two-tx envelope, heartbeat, timeout, post-commit cleanup (B16, B18, B25, R5, S27).<br>• **Deliver:** `@broker.task("ingest.pipeline")`. | T3.2a, T0.10 | [x] | Dev | W3 |
| T3.2i | Red | • **Achieve:** Verify heartbeat suppresses live-worker re-dispatch (S33, B16).<br>• **Deliver:** `tests/integration/test_worker_heartbeat.py`. | T3.2b, T2.2 | [x] | QA | W3 |
| T3.2j | Red | • **Achieve:** Pin pipeline-timeout failure path (S34, B18).<br>• **Deliver:** `tests/integration/test_pipeline_timeout.py`. | T3.2b | [x] | QA | W3 |
| T3.2k | Red | • **Achieve:** Pin CSV row-merger branch keyed on MIME (S35, B24).<br>• **Deliver:** `tests/integration/test_csv_row_merger.py`. | T3.2 | [x] | QA | W3 |
| T3.2l | Green | • **Achieve:** Implement CSV-only RowMerger branch (B24).<br>• **Deliver:** Pipeline factory adds `RowMerger` SuperComponent on `text/csv` branch only. | T3.2k | [x] | Dev | W3 |
| T3.2c | Red | • **Achieve:** Pin supersede semantics: per-loser commits, MAX(created_at) survives (P-C, S17–S22, S31).<br>• **Deliver:** `tests/integration/test_supersede_task.py`. | T3.2b, T2.10 | [x] | QA | W3 |
| T3.2d | Green | • **Achieve:** Implement supersede service + worker without holding K row locks across K cascades.<br>• **Deliver:** `services/ingest_service.py::supersede(document_id)` + `@broker.task("ingest.supersede")`. | T3.2c, T0.10 | [x] | Dev | W3 |
| T3.2e | Red | • **Achieve:** Guarantee retry idempotency — no duplicate chunks (R4, S25).<br>• **Deliver:** `tests/integration/test_pipeline_retry_idempotent.py`. | T3.2 | [x] | QA | W3 |
| T3.2f | Green | • **Achieve:** Implement idempotency-clean prefix.<br>• **Deliver:** Pipeline factory prepends idempotency-clean step. | T3.2e | [x] | Dev | W3 |
| T3.2g | Red | • **Achieve:** Pin acquire-NOWAIT contention path: fail fast, no `attempt` increment (R7, S28).<br>• **Deliver:** `tests/unit/test_worker_acquire_nowait.py`. | T3.2 | [x] | QA | W3 |
| T3.2h | Green | • **Achieve:** Implement NOWAIT + exponential backoff retry.<br>• **Deliver:** Worker uses `acquire_nowait`; on `LockNotAvailable` re-kiqs with exponential backoff (cap 30 s). | T3.2g | [x] | Dev | W3 |
| T3.3 | Red | • **Achieve:** Pin chat request validation, env-driven defaults, system auto-prepend, optional filters (B12, B22, B29).<br>• **Deliver:** `tests/unit/test_chat_request_schema.py`. | T4.6 | [x] | QA | W4 |
| T3.4 | Green | • **Achieve:** Implement `ChatRequest` schema + helpers per B21/B22/B29.<br>• **Deliver:** `src/ragent/schemas/chat.py`. | T3.3 | [x] | Dev | W4 |
| T3.5 | Red | • **Achieve:** Pin hybrid retrieval pipeline: title-aware embedding, BM25 over `icu_text`, filters, source hydration (B11, B15, B23, B26, B29, C4).<br>• **Deliver:** `tests/integration/test_chat_pipeline_retrieval.py`. | T2.4, T4.2, T4.4, T2.2, T1.12 | [x] | QA | W4 |
| T3.5a | Red | • **Achieve:** Pin pipeline-graph dispatch by `CHAT_JOIN_MODE` (C6).<br>• **Deliver:** `tests/unit/test_pipeline_factory_join_mode.py`. | T3.5 | [x] | QA | W4 |
| T3.6 | Green | • **Achieve:** Implement chat retrieval pipeline + shared retrieval utilities (B23 revised).<br>• **Deliver:** `src/ragent/pipelines/retrieve.py::build_retrieval_pipeline(join_mode)`. | T3.5, T3.5a | [x] | Dev | W4 |
| T3.7 | Red | • **Achieve:** Pin non-streaming LLM client contract (B28).<br>• **Deliver:** `tests/unit/test_llm_client_chat.py`. | T4.6 | [x] | QA | W4 |
| T3.8 | Green | • **Achieve:** Implement non-streaming `chat()` and ensure streaming surfaces usage.<br>• **Deliver:** `src/ragent/clients/llm.py::chat()`. | T3.7 | [x] | Dev | W4 |
| T3.9 | Red | • **Achieve:** Pin `POST /chat` non-streaming response shape (B5, B12, B13).<br>• **Deliver:** `tests/integration/test_chat_endpoint.py`. | T3.6, T3.8 | [x] | QA | W4 |
| T3.10 | Green | • **Achieve:** Implement non-streaming chat endpoint.<br>• **Deliver:** `src/ragent/routers/chat.py::POST /chat`. | T3.9 | [x] | Dev | W4 |
| T3.11 | Red | • **Achieve:** Pin SSE streaming framing (B6, B12, S6).<br>• **Deliver:** `tests/integration/test_chat_stream_endpoint.py`. | T3.6, T3.8 | [x] | QA | W4 |
| T3.12 | Green | • **Achieve:** Implement SSE chat stream endpoint.<br>• **Deliver:** `src/ragent/routers/chat.py::POST /chat/stream`. | T3.11 | [x] | Dev | W4 |
| T3.13 | Red | • **Achieve:** Pin fixed-window per-key rate-limit primitive against Redis (B27, B31).<br>• **Deliver:** `tests/unit/test_rate_limiter.py`. | T0.10, T0.9 | [x] | QA | W4 |
| T3.14 | Green | • **Achieve:** Implement RateLimiter adapter + composition wiring (B31).<br>• **Deliver:** `src/ragent/clients/rate_limiter.py`. | T3.13 | [x] | Dev | W4 |
| T3.15 | Red | • **Achieve:** Pin per-user rate-limit behavior end-to-end (B31, S37).<br>• **Deliver:** `tests/integration/test_chat_rate_limit.py`. | T3.14, T3.10, T3.12 | [x] | QA | W4 |
| T3.16 | Green | • **Achieve:** Apply rate-limit dependency to chat surfaces only (B31).<br>• **Deliver:** Wire `Depends(chat_rate_limit_dep)` onto chat router. | T3.15 | [x] | Dev | W4 |
| T3.17 | Red | • **Achieve:** Pin RAG message construction: grounding system message + context wrap.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py`. | T3.10, T3.12 | [x] | QA | W4 |
| T3.18 | Green | • **Achieve:** Implement RAG message builder + wire into routers.<br>• **Deliver:** `src/ragent/schemas/chat.py::build_rag_messages`. | T3.17 | [x] | Dev | W4 |
| T3.19 | Red | • **Achieve:** Pin standalone retrieve endpoint: filters, dedupe, excerpt truncated at router (§3.4.4, B23).<br>• **Deliver:** `tests/unit/test_retrieve_router.py`. | T3.6, T3.10 | [x] | QA | W4 |
| T3.20 | Green | • **Achieve:** Implement `POST /retrieve` using shared pipeline utilities.<br>• **Deliver:** `src/ragent/routers/retrieve.py`. | T3.19 | [x] | Dev | W4 |

---

### Track T4 — Third-Party Clients

**Counter: 完成 8 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T4.1 | Red | • **Achieve:** Pin token-manager refresh boundary, single-flight, J1-key body, ISO-8601 expiry, K8s SA mode (S9, P-F).<br>• **Deliver:** `tests/unit/test_token_manager.py`. | T0.6 | [x] | QA | W4 |
| T4.2 | Green | • **Achieve:** Implement TokenManager with single-flight refresh.<br>• **Deliver:** `src/ragent/clients/auth.py`. | T4.1 | [x] | Dev | W4 |
| T4.3 | Red | • **Achieve:** Pin embedding-client batching, asymmetric ingest/query timeouts, retry policy (B28, C8, P-B).<br>• **Deliver:** `tests/unit/test_embedding_client.py`. | T4.2 | [x] | QA | W4 |
| T4.4 | Green | • **Achieve:** Implement embedding client with two timeout paths.<br>• **Deliver:** `src/ragent/clients/embedding.py`. | T4.3 | [x] | Dev | W4 |
| T4.5 | Red | • **Achieve:** Pin LLM streaming contract + retry policy (B28).<br>• **Deliver:** `tests/unit/test_llm_client.py`. | T4.2 | [x] | QA | W4 |
| T4.6 | Green | • **Achieve:** Implement streaming LLM client.<br>• **Deliver:** `src/ragent/clients/llm.py`. | T4.5 | [x] | Dev | W4 |
| T4.7 | Red | • **Achieve:** Pin rerank client request shape (wired in P2).<br>• **Deliver:** `tests/unit/test_rerank_client.py`. | T4.2 | [x] | QA | W4 |
| T4.8 | Green | • **Achieve:** Implement rerank client.<br>• **Deliver:** `src/ragent/clients/rerank.py`. | T4.7 | [x] | Dev | W4 |

---

### Track T5 — Resilience (Reconciler)

**Counter: 完成 14 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T5.1 | Red | • **Achieve:** Pin PENDING re-dispatch + heartbeat suppression (S2, S33).<br>• **Deliver:** `tests/integration/test_reconciler_redispatch.py`. | T2.2, T2.8 | [x] | QA | W6 |
| T5.2 | Green | • **Achieve:** Implement one-shot Reconciler with K8s CronJob (B9).<br>• **Deliver:** `src/ragent/reconciler.py` + `deploy/k8s/reconciler-cronjob.yaml`. | T5.1 | [x] | Dev | W6 |
| T5.3 | Red | • **Achieve:** Pin attempt-budget exhaustion → FAILED with alert (S3).<br>• **Deliver:** `tests/integration/test_reconciler_failed.py`. | T5.2 | [x] | QA | W6 |
| T5.4 | Green | • **Achieve:** Implement FAILED transition + alert log line.<br>• **Deliver:** Status transition + structured log line `event=ingest.failed`. | T5.3 | [x] | Dev | W6 |
| T5.5 | Red | • **Achieve:** Pin DELETING resume idempotency (S13).<br>• **Deliver:** `tests/integration/test_reconciler_delete_resume.py`. | T2.10 | [x] | QA | W6 |
| T5.6 | Green | • **Achieve:** Implement DELETING resume.<br>• **Deliver:** Reconciler resumes DELETING. | T5.5 | [x] | Dev | W6 |
| T5.7 | Red | • **Achieve:** Pin UPLOADED-orphan re-dispatch (R1, S24).<br>• **Deliver:** `tests/integration/test_reconciler_uploaded_orphan.py`. | T2.8 | [x] | QA | W6 |
| T5.8 | Green | • **Achieve:** Implement UPLOADED-stale arm.<br>• **Deliver:** Reconciler arm for `UPLOADED > 5 min`. | T5.7 | [x] | Dev | W6 |
| T5.9 | Red | • **Achieve:** Pin multi-READY repair via supersede (R3, S26).<br>• **Deliver:** `tests/integration/test_reconciler_multi_ready_repair.py`. | T3.2d | [x] | QA | W6 |
| T5.10 | Green | • **Achieve:** Implement multi-READY detector arm.<br>• **Deliver:** Reconciler arm `GROUP BY source_id, source_app HAVING COUNT(*)>1`. | T5.9 | [x] | Dev | W6 |
| T5.11 | Red | • **Achieve:** Guarantee FAILED leaves no partial chunks/ES (R5, S27).<br>• **Deliver:** `tests/integration/test_reconciler_failed_cleanup.py`. | T5.4 | [x] | QA | W6 |
| T5.12 | Green | • **Achieve:** Wire fan-out cleanup into FAILED transition.<br>• **Deliver:** FAILED transition runs `fan_out_delete` + `delete_by_document_id` before commit. | T5.11 | [x] | Dev | W6 |
| T5.13 | Red | • **Achieve:** Pin Reconciler tick observability (R8, S30).<br>• **Deliver:** `tests/integration/test_reconciler_heartbeat.py`. | T5.2 | [x] | QA | W6 |
| T5.14 | Green | • **Achieve:** Implement tick counter + log line.<br>• **Deliver:** Heartbeat counter + log line in `reconciler.py`. | T5.13 | [x] | Dev | W6 |

---

### Track T6 — MCP Schema (501 in P1)

**Counter: 完成 2 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T6.1 | Structural | • **Achieve:** Reserve MCP surface in P1 with explicit 501 (§4.1.2).<br>• **Deliver:** `src/ragent/routers/mcp.py` exposing `POST /mcp/tools/rag` → 501. | T2.14 | [x] | Dev | W6 |
| T6.2 | Red | • **Achieve:** Pin P1 MCP 501 contract (S8).<br>• **Deliver:** `tests/unit/test_mcp_endpoint.py`. | T6.1 | [x] | QA | W6 |

---

### Track T7 — Observability + Acceptance

**Counter: 完成 16 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner | Week |
|---|---|---|---|:---:|---|:---:|
| T7.1 | Refactor | • **Achieve:** Wire OTEL traces + Prometheus metrics across api/worker/reconciler.<br>• **Deliver:** Haystack auto-trace + FastAPI middleware. | T3.4 | [x] | SRE | W6 |
| T7.1a | Red | • **Achieve:** Pin alerting on stalled Reconciler ticks (R8, S30).<br>• **Deliver:** `tests/integration/test_alerting_rules.py`. | T7.1 | [x] | QA | W6 |
| T7.1b | Behavioral | • **Achieve:** Adopt structlog for ISO 8601 / API trace / business / error logs with OTEL correlation.<br>• **Deliver:** `src/ragent/bootstrap/logging_config.py` + `src/ragent/middleware/logging.py` + instrumented routers/clients. | T7.1 | [x] | SRE | W7 |
| T7.2 | Acceptance | • **Achieve:** Validate the operator quickstart promise end-to-end (B30).<br>• **Deliver:** `tests/e2e/test_ingest_success_rate.py` — 100 docs ≥ 99% READY within 60 s each. | T3.2, T5.6, T7.5d, T7.5e, T0.11 | [x] | QA | W6 |
| T7.3 | Acceptance | • **Achieve:** Hit golden-set retrieval target on the live API process (C7).<br>• **Deliver:** `tests/e2e/test_golden_set.py` — top-3 ≥ 70% on 50 Q/A rows. | T3.4, T7.2 | [x] | QA | W6 |
| T7.4 | Acceptance | • **Achieve:** Validate chaos recovery SLA.<br>• **Deliver:** Chaos drill: kill worker mid-ingest → Reconciler recovers ≤ 10 min. | T5.6 | [x] | SRE | W6 |
| T7.5 | Structural | • **Achieve:** Refuse to boot in any unsafe P1 OPEN configuration (B28).<br>• **Deliver:** `src/ragent/bootstrap/guard.py`. | T2.14 | [x] | SRE | W6 |
| T7.5a | Structural | • **Achieve:** Single composition root that owns all env reads + DI graph (B17, B30, B31, C6).<br>• **Deliver:** `src/ragent/bootstrap/composition.py`. | T1.7, T1.12, T2.2, T2.4, T2.6, T3.6, T3.8, T3.14, T4.2, T4.4, T4.6, T4.8 | [x] | Dev | W6 |
| T7.5b | Red | • **Achieve:** Verify composition root builds a fully-wired graph eagerly (B17, B30).<br>• **Deliver:** `tests/integration/test_composition_root.py`. | T7.5a | [x] | QA | W6 |
| T7.5c | Structural | • **Achieve:** Implement the FastAPI app factory with lifespan-driven init + RFC 9457 errors + `X-User-Id` middleware.<br>• **Deliver:** `src/ragent/bootstrap/app.py::create_app()`. | T7.5, T7.5a, T0.8d, T2.14, T3.10, T3.12, T7.8 | [x] | Dev | W6 |
| T7.5d | Structural | • **Achieve:** Provide the single API entrypoint operators run.<br>• **Deliver:** `src/ragent/api.py`. | T7.5c | [x] | Dev | W6 |
| T7.5e | Structural | • **Achieve:** Provide the single worker entrypoint operators run.<br>• **Deliver:** `src/ragent/worker.py`. | T0.10, T7.5a, T0.8d, T3.2b, T3.2d | [x] | Dev | W6 |
| T7.5f | Red | • **Achieve:** Verify app factory boots end-to-end with lifespan + middleware.<br>• **Deliver:** `tests/integration/test_app_factory.py`. | T7.5c | [x] | QA | W6 |
| T7.6 | Red | • **Achieve:** Pin startup-guard refusals.<br>• **Deliver:** `tests/unit/test_bootstrap_startup_guard.py`. | T7.5 | [x] | QA | W6 |
| T7.7 | Red | • **Achieve:** Pin health/metrics surfaces and dependency probes (B4, B26, B27, B28, C9).<br>• **Deliver:** `tests/integration/test_health_endpoints.py`. | T7.1 | [x] | QA | W6 |
| T7.8 | Green | • **Achieve:** Implement health + metrics endpoints with per-dep timeouts and middleware bypass (B27, B28, C9).<br>• **Deliver:** `src/ragent/routers/health.py`. | T7.7 | [x] | Dev | W6 |

---

### Track T8 — Authentication & Permission Layer

> P1 produced NO code in this track. JWT (T8.0–T8.5a, T8.D1–T8.D3) shipped in P2. Permission layer (T8.3–T8.9) descoped.

**Counter: 完成 10 / 未完成 0 / descope 7**

| # | Category | Task | Depends On | Status | Owner | Phase |
|---|---|---|---|:---:|---|:---:|
| T8.0 | Structural | • **Achieve:** Centralise the `X-User-Id` literal in `bootstrap/app.py`. | (entry) | [x] | Dev | P2 |
| T8.1 | Red | • **Achieve:** ✱ Superseded by T8.1a — decode-only contract dropped. | T8.0 | [x] | QA | P2 |
| T8.1a | Red | • **Achieve:** Pin Armasec-verified JWT contract (§3.5 rewritten 2026-05-20). | T8.0 | [x] | QA | P2 |
| T8.2 | Green | • **Achieve:** ✱ Superseded by T8.2a — decode-only implementation replaced. | T8.1 | [x] | Dev | P2 |
| T8.2a | Green | • **Achieve:** Replace decode-only with Armasec JWKS verification. | T8.1a | [x] | Dev | P2 |
| T8.3a | Red+Green | • **Achieve:** Codify the public-path bypass as a single named constant. | T8.2a | [x] | Dev | P2 |
| T8.5a | Structural | • **Achieve:** Replace Armasec with joserfc + explicit httpx.Client injection. | T8.3a | [x] | Dev | P2 |
| T8.D3 | Red+Green | • **Achieve:** Anti-drift CI lint — fail collection if any router redeclares an auth header. | T8.D2 | [x] | QA | P2 |
| T8.D2 | Red+Green | • **Achieve:** Single source of truth for `user_id` in route handlers. | T8.D1 | [x] | Dev | P2 |
| T8.D1 | Red+Green | • **Achieve:** End Swagger doc drift — auth header in `/openapi.json` derived from runtime config. | T8.5a | [x] | Dev | P2 |
| T8.3 | Red | • **Achieve:** Pin Permission Protocol surface. | T8.2 | [~] | QA | P2 |
| T8.4 | Green | • **Achieve:** Implement Permission Protocol + OpenFGA adapter (B14). | T8.3 | [~] | Dev | P2 |
| T8.5 | Red | • **Achieve:** Pin chat permission gate as opt-in post-retrieval filter. | T8.4 | [~] | QA | P2 |
| T8.6 | Green | • **Achieve:** Wire opt-in chat permission gate. | T8.5 | [~] | Dev | P2 |
| T8.7 | Red | • **Achieve:** Pin ingest permission gate behavior + `create_user` always recorded. | T8.4 | [~] | QA | P2 |
| T8.8 | Green | • **Achieve:** Wire opt-in ingest permission gate at three call sites. | T8.7 | [~] | Dev | P2 |
| T8.9 | Behavioral | • **Achieve:** Resolve employee identity + write OpenFGA tuples on ingest. | T8.2, T8.4 | [~] | Dev | P2 |

---

## Phase 1 v2 Tracks

---

### Track T-MCP — MCP JSON-RPC 2.0 Server (P2.5) — 2026-05-11

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Depends On | Status | Owner |
|---|---|---|---|:---:|---|
| T-MCP.1 | Red | • **Achieve:** Pin JSON-RPC 2.0 envelope contract (parse error, invalid request, method not found, notification).<br>• **Deliver:** `tests/unit/test_mcp_envelope.py`. | — | [x] | QA |
| T-MCP.2 | Green | • **Achieve:** Implement JSON-RPC dispatcher skeleton.<br>• **Deliver:** `src/ragent/routers/mcp.py::create_mcp_router()`. | T-MCP.1 | [x] | Dev |
| T-MCP.3 | Red | • **Achieve:** Pin `initialize` handshake (S58).<br>• **Deliver:** `tests/unit/test_mcp_initialize.py`. | T-MCP.2 | [x] | QA |
| T-MCP.4 | Green | • **Achieve:** Implement `initialize` handler.<br>• **Deliver:** `mcp.py::_handle_initialize(params)`. | T-MCP.3 | [x] | Dev |
| T-MCP.5 | Red | • **Achieve:** Pin `tools/list` contract (S59) — exactly one tool `retrieve`.<br>• **Deliver:** `tests/unit/test_mcp_tools_list.py`. | T-MCP.2 | [x] | QA |
| T-MCP.6 | Green | • **Achieve:** Implement `tools/list` returning the retrieve tool.<br>• **Deliver:** `mcp.py::_RETRIEVE_TOOL_SCHEMA` + `_handle_tools_list()`. | T-MCP.5 | [x] | Dev |
| T-MCP.7 | Red | • **Achieve:** Pin `tools/call retrieve` happy path (S60).<br>• **Deliver:** `tests/unit/test_mcp_tools_call_retrieve.py`. | T-MCP.2 | [x] | QA |
| T-MCP.8 | Green | • **Achieve:** Implement `tools/call` dispatching to `run_retrieval`.<br>• **Deliver:** `mcp.py::_handle_tools_call`. | T-MCP.7 | [x] | Dev |
| T-MCP.9 | Red | • **Achieve:** Pin all `tools/call` error paths (S62, S63, S67).<br>• **Deliver:** `tests/unit/test_mcp_tools_call_errors.py`. | T-MCP.8 | [x] | QA |
| T-MCP.10 | Green | • **Achieve:** Add input schema validation + tool name dispatch + pipeline-failure mapper.<br>• **Deliver:** `mcp.py::_validate_retrieve_args(args)`. | T-MCP.9 | [x] | Dev |
| T-MCP.11 | Red | • **Achieve:** End-to-end through TestClient + real `build_retrieval_pipeline`.<br>• **Deliver:** `tests/integration/test_mcp_router.py`. | T-MCP.4, T-MCP.6, T-MCP.10 | [x] | QA |
| T-MCP.12 | Refactor | • **Achieve:** Remove P1 stub endpoint and update docs.<br>• **Deliver:** Delete `POST /mcp/v1/tools/rag` 501 route; `docs/API.md` documents `/mcp/v1`. | T-MCP.11 | [x] | Dev |

---

### Track TA — aiomysql Adoption (async DB layer) — 2026-05-06

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| TA.1 | Red | • **Achieve:** Pin async contract for DocumentRepository and ChunkRepository. | [x] | QA |
| TA.2 | Green | • **Achieve:** Convert both repos to `async def` with SQLAlchemy `AsyncEngine`. | [x] | Dev |
| TA.3 | Red | • **Achieve:** Pin async IngestService contract. | [x] | QA |
| TA.4 | Green | • **Achieve:** Convert IngestService to async. | [x] | Dev |
| TA.5 | Red | • **Achieve:** Pin router drops `run_in_threadpool`. | [x] | QA |
| TA.6 | Green | • **Achieve:** Simplify ingest router to direct `await` calls. | [x] | Dev |
| TA.7 | Red | • **Achieve:** Pin reconciler fully-async contract. | [x] | QA |
| TA.8 | Green | • **Achieve:** Convert Reconciler to fully async. | [x] | Dev |
| TA.9 | Red | • **Achieve:** Pin ingest worker direct `await` on repos. | [x] | QA |
| TA.10 | Green | • **Achieve:** Refactor ingest worker to `await` repos directly. | [x] | Dev |
| TA.11 | Green | • **Achieve:** Wire async engine in composition root + native async health probe. | [x] | Dev |
| TA.12 | Refactor | • **Achieve:** Green tests stay green after structural cleanup. | [x] | Reviewer |

---

### Track T2v — Phase 1 v2 Ingest API Refactor — 2026-05-06

**Counter: 完成 26 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T2v.20 | Structural | • **Achieve:** Add v2 documents columns + drop chunks table.<br>• **Deliver:** Alembic revision + schema-drift test green + `migrations/schema.sql`. | [x] | Dev |
| T2v.21 | Structural | • **Achieve:** Add ES `raw_content` field to `chunks_v1`.<br>• **Deliver:** `resources/es/chunks_v1.json` updated. | [x] | Dev |
| T2v.22 | Red | • **Achieve:** Pin v2 request schema (discriminated union + validators). | [x] | QA |
| T2v.23 | Green | • **Achieve:** Implement Pydantic discriminated request models. | [x] | Dev |
| T2v.24 | Red | • **Achieve:** Pin v2 router contract (JSON only, no multipart). | [x] | QA |
| T2v.25 | Green | • **Achieve:** Implement v2 router (JSON-only). | [x] | Dev |
| T2v.26 | Red | • **Achieve:** Pin service `create` branching contract. | [x] | QA |
| T2v.27 | Green | • **Achieve:** Implement branched create + structured business log. | [x] | Dev |
| T2v.28 | Red | • **Achieve:** Pin `MinioSiteRegistry` semantics. | [x] | QA |
| T2v.29 | Green | • **Achieve:** Implement registry + composition wiring. | [x] | Dev |
| T2v.30 | Red | • **Achieve:** Pin `_TextLoader` Haystack component. | [x] | QA |
| T2v.31 | Green | • **Achieve:** Implement `_TextLoader`. | [x] | Dev |
| T2v.32 | Red | • **Achieve:** Pin `_MarkdownASTSplitter` (mistletoe). | [x] | QA |
| T2v.33 | Green | • **Achieve:** Implement `_MarkdownASTSplitter` via mistletoe AST walk. | [x] | Dev |
| T2v.34 | Red | • **Achieve:** Pin `_HtmlASTSplitter` (selectolax). | [x] | QA |
| T2v.35 | Green | • **Achieve:** Implement `_HtmlASTSplitter` via selectolax DOM walk. | [x] | Dev |
| T2v.36 | Red | • **Achieve:** Pin `_BudgetChunker` (mime-agnostic, 1000/1500/100). | [x] | QA |
| T2v.37 | Green | • **Achieve:** Implement `_BudgetChunker`. | [x] | Dev |
| T2v.38 | Red | • **Achieve:** Pin `FileTypeRouter` wiring + unroutable failure path. | [x] | QA |
| T2v.39 | Green | • **Achieve:** Wire pipeline graph end-to-end. | [x] | Dev |
| T2v.40 | Red | • **Achieve:** Pin chat read-path uses `raw_content` with `content` fallback. | [x] | QA |
| T2v.41 | Green | • **Achieve:** Implement chat read-path + `source_url` in citations. | [x] | Dev |
| T2v.42 | Red | • **Achieve:** Pin per-step business + failure logs (Logging Rule extension). | [x] | QA |
| T2v.43 | Green | • **Achieve:** Wire structlog per-step events + correlate via OTEL. | [x] | Dev |
| T2v.44 | Refactor | • **Achieve:** Delete dead v1 code. | [x] | Dev |
| T2v.45 | Acceptance | • **Achieve:** Golden end-to-end test with wiremock embedding + testcontainers. | [x] | QA |

---

### Track T-SR — Source-id Review Follow-up — 2026-05-06

**Counter: 完成 7 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-SR.1 | Behavioral | • **Achieve:** Cascade ES chunk delete when supersede picks a loser. | [x] | Dev |
| T-SR.2 | Structural | • **Achieve:** Capture the surrogate-PK + biz-UNIQUE rule and lock the revision-model decisions. | [x] | Architect |
| T-SR.3 | Structural | • **Achieve:** Rename `documents.source_workspace` → `source_meta` and widen to `VARCHAR(1024)`. | [x] | Dev |
| T-SR.4 | Behavioral | • **Achieve:** DB-side survivor election in `pop_oldest_loser_for_supersede`. | [x] | Dev |
| T-SR.5 | Behavioral | • **Achieve:** Hydration surfaces only `READY` rows. | [x] | Dev |
| T-SR.6 | Structural | • **Achieve:** Auto-create configured MinIO bucket(s) at boot. | [x] | Dev |
| T-SR.7 | Structural | • **Achieve:** Split test tiers — `make test-gate` excludes e2e. | [x] | Dev |

---

### Track T-ICU — ICU Analyzer Convergence (T-ICU.1–T-ICU.3)

> T-ICU.4 (remaining manual smoke test) is tracked in `docs/00_plan.md`.

**Counter: 完成 3 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-ICU.1 | Structural | • **Achieve:** Reconcile spec §5.2 with B26. | [x] | Dev |
| T-ICU.2 | Red | • **Achieve:** Pin ICU analyzer in prod mapping; pin standard analyzer in test mapping. | [x] | QA |
| T-ICU.3 | Green | • **Achieve:** Implement env-driven mapping dir + commit two mapping files. | [x] | Dev |

---

### Track T-RR — Reconciler-as-safety-net Follow-up — 2026-05-08

**Counter: 完成 18 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-RR.1 | Red | • **Achieve:** Pin `_SourceHydrator` drop-on-miss semantics (B36 / S6j). | [x] | QA |
| T-RR.2 | Green | • **Achieve:** Implement drop-on-miss in hydrator. | [x] | Dev |
| T-RR.3 | Refactor | • **Achieve:** Update existing chat-pipeline tests. | [x] | Dev |
| T-RR.4 | Red | • **Achieve:** Pin composition no longer requires legacy `MINIO_ENDPOINT` vars when `MINIO_SITES` is set. | [x] | QA |
| T-RR.5 | Structural | • **Achieve:** Switch `/readyz` minio probe source from legacy `MinIOClient` to registry default site. | [x] | Dev |
| T-RR.6 | Green | • **Achieve:** Remove unconditional `_require` of legacy MinIO vars when `MINIO_SITES` is set. | [x] | Dev |
| T-RR.7 | Red | • **Achieve:** Pin AI token boot-time pre-warm. | [x] | QA |
| T-RR.8 | Green | • **Achieve:** Pre-warm tokens in lifespan startup. | [x] | Dev |
| T-RR.9 | Red | • **Achieve:** Pin worker's atomic promote-and-demote on READY (B39). | [x] | QA |
| T-RR.10 | Green | • **Achieve:** Implement atomic promote-demote in repository. | [x] | Dev |
| T-RR.11 | Red | • **Achieve:** Pin HTTP `DELETE /ingest/{id}` actually runs `fan_out_delete`. | [x] | QA |
| T-RR.12 | Structural | • **Achieve:** Inject `PluginRegistry` into `IngestService`. | [x] | Dev |
| T-RR.13 | Green | • **Achieve:** Replace `_has_fan_out` introspection with explicit registry call. | [x] | Dev |
| T-RR.14 | Red | • **Achieve:** Pin worker promote is DB-arbitrated by `MAX(created_at)`. | [x] | QA |
| T-RR.15 | Green | • **Achieve:** Implement DB-side survivor election in worker promote. | [x] | Dev |
| T-RR.16 | Red | • **Achieve:** Pin that post-READY enrichment (`fan_out`) does NOT run when the worker self-demotes. | [x] | QA |
| T-RR.17 | Green | • **Achieve:** Gate worker `fan_out` on promote outcome. | [x] | Dev |
| T-RR.18 | Red | • **Achieve:** Pin the `FOR UPDATE` lock semantic. | [x] | QA |

---

### Track T-EF / T-AV — Retrieve/Ingest Enhancements + Versioning — 2026-05-11

**Counter: 完成 6 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-EF.1 | Behavioral | • **Achieve:** Add `top_k`/`min_score` to `POST /retrieve`; expose `source_meta`. | [x] | Dev |
| T-EF.2 | Behavioral | • **Achieve:** Add `source_id`/`source_app` filter params to `GET /ingest` list. | [x] | Dev |
| T-EF.3 | Behavioral | • **Achieve:** Fix `min_score` — apply as post-retrieval filter. | [x] | Dev |
| T-EF.4 | Behavioral | • **Achieve:** Enforce `top_k` as a hard post-pipeline cap in `run_retrieval()`. | [x] | Dev |
| T-EF.5 | Behavioral | • **Achieve:** Expose retrieval score in `POST /retrieve` chunk response. | [x] | Dev |
| T-AV.1 | Behavioral | • **Achieve:** Add `/v1` version segment to all business API paths. | [x] | Dev |

---

### Track T-BL — Binary Document Loaders (DOCX/PPTX) — 2026-05-12

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-BL.1 | Red | • **Achieve:** Pin `_DocxASTSplitter` atom contract. | [x] | QA |
| T-BL.2 | Red | • **Achieve:** Pin `_PptxASTSplitter` atom contract. | [x] | QA |
| T-BL.3 | Green | • **Achieve:** Implement `_DocxASTSplitter` and `_PptxASTSplitter`. | [x] | Dev |
| T-BL.4 | Acceptance | • **Achieve:** `_MimeAwareSplitter` dispatch covers all new routes. | [x] | QA |
| T-BL.5 | Structural | • **Achieve:** Address Gemini/Codex PR review findings. | [x] | Dev |
| T-BL.6 | Behavioral | • **Achieve:** Accept short aliases `pptx`/`docx` at all API entry points. | [x] | Dev |
| T-BL.7 | Behavioral | • **Achieve:** Reject binary MIME on `ingest_type=inline` at schema validation time. | [x] | Dev |
| T-BL.8 | Behavioral | • **Achieve:** Worker uses `doc.mime_type` (DB) as authoritative MIME routing key. | [x] | Dev |
| T-BL.9 | Behavioral | • **Achieve:** Case-insensitive MIME handling per RFC 2045 §5.1. | [x] | Dev |
| T-BL.10 | Behavioral | • **Achieve:** Fix `mime_type=None` in all `ingest.step.*` structured logs for PPTX/DOCX. | [x] | Dev |
| T-BL.11 | Behavioral | • **Achieve:** Log `file_size_bytes` in the load step and `splitter` name in split step. | [x] | Dev |
| T-BL.12 | Behavioral | • **Achieve:** Ensure `mime_type` appears in all `ingest.step.*` logs for legacy rows. | [x] | Dev |

---

### Track T-FIL — Ingest Pipeline Bug Fixes — 2026-05-12

**Counter: 完成 6 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-FIL.1 | Behavioral | • **Achieve:** Fix `head_object` `or 0` bug. | [x] | Dev |
| T-FIL.2 | Behavioral | • **Achieve:** Enforce `INGEST_FILE_MAX_BYTES` limit for `ingest_type=file` ingests. | [x] | Dev |
| T-FIL.3 | Behavioral | • **Achieve:** Replace `SELECT … FOR UPDATE` with lock-free atomic correlated-subquery UPDATE. | [x] | Dev |
| T-FIL.4 | Behavioral | • **Achieve:** Verify `ingest_type=file` worker never calls `delete_object`. | [x] | Dev |
| T-FIL.5 | Behavioral | • **Achieve:** Fix `_record_file` false `ObjectNotFoundError` for files with unknown size metadata. | [x] | Dev |
| T-FIL.6 | Behavioral | • **Achieve:** Guard `_log_transition("PENDING", "DELETING")` on actual row change. | [x] | Dev |

---

### Track T-UP — Unprotect API Integration — 2026-05-13 / 2026-05-21

**Counter: 完成 5 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-UP.1 | Red | • **Achieve:** Pin `UnprotectClient` contract. | [x] | QA |
| T-UP.2 | Red | • **Achieve:** Pin worker unprotect-gate behaviour. | [x] | QA |
| T-UP.3 | Green | • **Achieve:** Implement `UnprotectClient` and wire into composition root and worker. | [x] | Dev |
| T-UP.4 | Red | • **Achieve:** Pin inline ingest unprotect skip + failure fallback. | [x] | QA |
| T-UP.5 | Green | • **Achieve:** Implement inline skip and fallback. | [x] | Dev |

---

### Track T-PDF — PDF Ingest Support — 2026-05-13

**Counter: 完成 5 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-PDF.1 | Red | • **Achieve:** Pin `_PdfASTSplitter` atom contract. | [x] | QA |
| T-PDF.2 | Green | • **Achieve:** Implement `_PdfASTSplitter` and helper `_pdf_page_text`. | [x] | Dev |
| T-PDF.3 | Green | • **Achieve:** Wire `application/pdf` end-to-end through schema, factory, and existing tests. | [x] | Dev |
| T-PDF.4 | Refactor | • **Achieve:** Address post-review findings: remove redundant batch loop; OCR language list env-configurable. | [x] | Dev |
| T-PDF.5 | Green | • **Achieve:** Implement PyMuPDF best-practice OOM prevention. | [x] | Dev |

---

### Track T-RERUN — Manual Rerun Endpoint — 2026-05-14

**Counter: 完成 3 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-RERUN.1 | Red+Green | • **Achieve:** Add `DocumentRepository.mark_for_rerun(document_id)`. | [x] | Dev |
| T-RERUN.2 | Red+Green | • **Achieve:** Add `IngestService.rerun(document_id)` and `DocumentNotRerunnable` exception. | [x] | Dev |
| T-RERUN.3 | Red+Green | • **Achieve:** Add `POST /ingest/v1/{document_id}/rerun` returning 202 / 404 / 409. | [x] | Dev |

---

### Track T-HTTPLOG — HTTP Upstream Error Logging — 2026-05-14

**Counter: 完成 3 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-HTTPLOG.1 | Red | • **Achieve:** Pin `install_error_logging(client, ...)` contract. | [x] | QA |
| T-HTTPLOG.2 | Green | • **Achieve:** Implement the hook factory and wire it into both shared httpx clients. | [x] | Dev |
| T-HTTPLOG.3 | Refactor | • **Achieve:** `/simplify` + `/review` pass; journal-add row recording the deliberate `http_request_payload` / `http_response_payload` denylist carve-out. | [x] | Dev |

---

### Track T-SEC — Security File-Upload Checks — 2026-05-14

**Counter: 完成 8 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-SEC.1 | Red | • **Achieve:** Pin magic-byte rejection at `POST /ingest/v1/upload`. | [x] | QA |
| T-SEC.2 | Green | • **Achieve:** Implement magic-byte validator at upload route. | [x] | Dev |
| T-SEC.3 | Red | • **Achieve:** Pin zip-archive preflight contract for DOCX/PPTX. | [x] | QA |
| T-SEC.4 | Green | • **Achieve:** Implement `assert_safe_zip` and wire into DOCX/PPTX splitters. | [x] | Dev |
| T-SEC.5 | Red | • **Achieve:** Pin PDF page-count cap before per-page extraction. | [x] | QA |
| T-SEC.6 | Green | • **Achieve:** Implement page-count guard in `_PdfASTSplitter` + env var. | [x] | Dev |
| T-SEC.7 | Behavioral | • **Achieve:** Expose Prometheus counter for guard rejections. | [x] | Dev |
| T-SEC.8 | Refactor | • **Achieve:** Update spec + env-var inventory. | [x] | Dev |

---

### Track T-OCR — Replace Tesseract with RapidOCR — 2026-05-21

**Counter: 完成 4 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-OCR.1 | Red | • **Achieve:** Update OCR tests to mock `_get_rapidocr_engine`. | [x] | QA |
| T-OCR.2 | Green | • **Achieve:** Add `rapidocr-onnxruntime`; rewrite `_pdf_page_text()` to use RapidOCR. | [x] | Dev |
| T-OCR.3 | Refactor | • **Achieve:** Update spec + remove `PDF_OCR_LANGUAGES` env-var row. | [x] | Dev |
| T-OCR.4 | Refactor | • **Achieve:** Use `pymupdf4llm.to_markdown` per page; remove `_rapidocr_engine` singleton. | [x] | Dev |

---

### Track T-HDR — Header/Footer Exclusion (PDF + PPTX)

**Counter: 完成 2 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-HDR.1 | Behavioral | • **Achieve:** PDF: add `INGEST_PDF_MARGIN_PTS` margin-based exclusion. | [x] | Dev |
| T-HDR.2 | Behavioral | • **Achieve:** PPTX: filter `PP_PLACEHOLDER.FOOTER / DATE / SLIDE_NUMBER` shapes. | [x] | Dev |

---

### Track T-EM — Embedding-Model Lifecycle — 2026-05-15

**Counter: 完成 22 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-EM.0 | Analysis | • **Achieve:** Lock the multi-vector single-index swap design and APIs. | [x] | Architect |
| T-EM.1 | Red | • **Achieve:** Pin the embedding-lifecycle state machine. | [x] | QA |
| T-EM.2 | Green | • **Achieve:** Implement the embedding-lifecycle state machine. | [x] | Dev |
| T-EM.3 | Red | • **Achieve:** Pin `EmbeddingModelConfig` dataclass. | [x] | QA |
| T-EM.4 | Green | • **Achieve:** Implement `EmbeddingModelConfig`. | [x] | Dev |
| T-EM.5 | Structural | • **Achieve:** Persist lifecycle settings in MariaDB. | [x] | Dev |
| T-EM.6 | Red | • **Achieve:** Pin `SystemSettingsRepository` contract. | [x] | QA |
| T-EM.7 | Green | • **Achieve:** Implement repository. | [x] | Dev |
| T-EM.8 | Red | • **Achieve:** Pin `ActiveModelRegistry` cache contract. | [x] | QA |
| T-EM.9 | Green | • **Achieve:** Implement `ActiveModelRegistry`. | [x] | Dev |
| T-EM.10 | Red | • **Achieve:** Pin cutover preflight. | [x] | QA |
| T-EM.11 | Green | • **Achieve:** Implement preflight. | [x] | Dev |
| T-EM.12 | Red | • **Achieve:** Pin admin router for five lifecycle endpoints. | [x] | QA |
| T-EM.13 | Green | • **Achieve:** Implement admin router. | [x] | Dev |
| T-EM.14 | Red | • **Achieve:** Pin ingest dual-write. | [x] | QA |
| T-EM.15 | Green | • **Achieve:** Implement dual-write embedder. | [x] | Dev |
| T-EM.16 | Red | • **Achieve:** Pin query path uses `registry.read_model()`. | [x] | QA |
| T-EM.17 | Green | • **Achieve:** Implement dynamic field selection in `_QueryEmbedder`. | [x] | Dev |
| T-EM.18 | Red | • **Achieve:** Pin retired-field reconciler arm. | [x] | QA |
| T-EM.19 | Green | • **Achieve:** Implement reconciler arm. | [x] | Dev |
| T-EM.20 | Red | • **Achieve:** End-to-end lifecycle integration test. | [x] | QA |
| T-EM.21 | Green | • **Achieve:** Wire `ActiveModelRegistry` into composition root. | [x] | Dev |

---

### Track T-EM-R — Embedding Lifecycle Rework (index-per-model + alias cutover) — 2026-05-21

**Counter: 完成 10 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-EM-R.1 | Red+Green | • **Achieve:** Rework index-per-model design (T-EM.4, T-EM.9). | [x] | Dev |
| T-EM-R.2 | Red+Green | • **Achieve:** Alias cutover mechanism. | [x] | Dev |
| T-EM-R.3 | Red+Green | • **Achieve:** Candidate index write path. | [x] | Dev |
| T-EM-R.4 | Red+Green | • **Achieve:** Cutover validation. | [x] | Dev |
| T-EM-R.5 | Red+Green | • **Achieve:** Stable index read path. | [x] | Dev |
| T-EM-R.6 | Red+Green | • **Achieve:** Registry refresh and staleness check. | [x] | Dev |
| T-EM-R.7 | Red+Green | • **Achieve:** Dual-write path updated for index-per-model. | [x] | Dev |
| T-EM-R.8 | Red+Green | • **Achieve:** Reconciler arm for retired index cleanup. | [x] | Dev |
| T-EM-R.9 | Red+Green | • **Achieve:** Composition root wiring for reworked registry. | [x] | Dev |
| T-EM-R.10 | Red+Green | • **Achieve:** End-to-end lifecycle integration test for new design. | [x] | QA |

---

### Track T-FB — Feedback Retrieval Signal — 2026-05-16

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-FB.1 | Red+Green | • **Achieve:** Feedback schema and storage (B55). | [x] | Dev |
| T-FB.2 | Red+Green | • **Achieve:** Feedback endpoint (B54). | [x] | Dev |
| T-FB.3 | Structural | • **Achieve:** Feedback model and repository wiring. | [x] | Dev |
| T-FB.4 | Red+Green | • **Achieve:** Feedback retrieval component. | [x] | QA/Dev |
| T-FB.5 | Structural | • **Achieve:** ES feedback index and mapping. | [x] | Dev |
| T-FB.6 | Red+Green | • **Achieve:** Feedback RRF signal integration test. | [x] | QA/Dev |
| T-FB.7 | Red+Green | • **Achieve:** Feedback memory retriever integration. | [x] | QA/Dev |
| T-FB.8 | Structural | • **Achieve:** Chat pipeline updated to include feedback signal. | [x] | Dev |
| T-FB.9 | Red+Green | • **Achieve:** Feedback signal end-to-end pipeline test. | [x] | QA/Dev |
| T-FB.10 | Red+Green | • **Achieve:** Feedback dedup and score normalization. | [x] | QA/Dev |
| T-FB.11 | Red+Green | • **Achieve:** Acceptance test — feedback improves recall over control. | [x] | QA |
| T-FB.12 | Refactor | • **Achieve:** Simplify feedback pipeline wiring. | [x] | Dev |

---

### Track T-IUP — Ingest Upload Discriminator Fix — 2026-05-19

**Counter: 完成 2 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-IUP.1 | Red | • **Achieve:** Pin the new discriminator + cleanup contract on the service layer. | [x] | QA |
| T-IUP.2 | Green | • **Achieve:** Add the `upload` enum value end-to-end and wire the new cleanup gate. | [x] | Dev |

---

### Track T-EI — ES Chunks Index Config + Housekeeping

**Counter: 完成 7 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-EI.1 | Structural | • **Achieve:** Audit and correct ES index settings vs spec. | [x] | Dev |
| T-EI.2 | Red | • **Achieve:** Pin ES index config test. | [x] | QA |
| T-EI.2a | Red+Green | • **Achieve:** ES env audit test. | [x] | Dev |
| T-EI.3 | Green | • **Achieve:** Implement corrected ES index config. | [x] | Dev |
| T-EI.4 | Red+Green | • **Achieve:** ES config integration test. | [x] | QA |
| T-EI.5 | Spec | • **Achieve:** Sync spec §5.2 with corrected settings. | [x] | Spec |
| T-EI.6 | Red+Green | • **Achieve:** Address PR #83 review findings. | [x] | Dev |

---

### Track T-APL — API Pipeline Param Sanity & Observability — 2026-05-19

**Counter: 完成 11 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-APL.1 | Red | • **Achieve:** Pin per-request `top_k` reaches `_Reranker.run` and `_FeedbackMemoryRetriever.run`. | [x] | QA |
| T-APL.2 | Green | • **Achieve:** Thread per-request `top_k` to both components. | [x] | Dev |
| T-APL.3 | Red | • **Achieve:** Pin explicit-`0` constructor kwargs on clients are honoured. | [x] | QA |
| T-APL.4 | Green | • **Achieve:** Replace `value or env_default` with `value if value is not None else env_default`. | [x] | Dev |
| T-APL.5 | Structural | • **Achieve:** Move module-level env reads to composition root; inject as constructor kwargs. | [x] | Dev |
| T-APL.6 | Red | • **Achieve:** Pin chat pipeline component observability events. | [x] | QA |
| T-APL.7 | Green | • **Achieve:** Extract `wrap_component_run` into generic `wrap_pipeline_component` helper. | [x] | Dev |
| T-APL.8 | Red | • **Achieve:** Pin `request_id` propagation across TaskIQ boundary. | [x] | QA |
| T-APL.9 | Green | • **Achieve:** Implement `taskiq.TaskiqMiddleware` subclass for context propagation. | [x] | Dev |
| T-APL.10 | Structural | • **Achieve:** Drop `wrap_component_run` back-compat alias. | [x] | Dev |
| T-APL.11 | Red+Green | • **Achieve:** Each wrapped `run()` opens an OTEL span. | [x] | Dev |

---

### Track T-EF-CLEAN — ES Embedding Field Name Clarification

**Counter: 完成 3 / 未完成 0 / descope 0**

| # | Category | Task | Spec | Status | Owner |
|---|---|---|:-:|:-:|---|
| T-EF-CLEAN.1 | Structural | • **Achieve:** Fix misleading `_QueryEmbedder` docstring that claimed registry mode targets `embedding_<m>_<d>` fields. | B61 | [x] | Dev |
| T-EF-CLEAN.2 | Structural | • **Achieve:** Remove dead `_REGISTRY_MODEL_FIELD` constant and the `PUT /_mapping` block from test `es_store` fixture. | B61 | [x] | Dev |
| T-EF-CLEAN.3 | Structural | • **Achieve:** Document index-per-model design supersession of B50 in `docs/spec/decision_log.md`. | B61/B60 | [x] | Dev |

---

## Phase 1 → Phase 2 Spillover

> Both rows descoped: live-AI gate and full chaos suite require P2 infrastructure.

**Counter: 完成 0 / 未完成 0 / descope 2**

| # | Category | Task | Depends On | Status | Owner | Phase |
|---|---|---|---|:---:|---|:---:|
| T7.3.x | Acceptance | • **Achieve:** Wire T7.3 retrieval-recall SLO to a real automated gate against live endpoints.<br>• **Deliver:** Scheduled CI job with real API secrets; `test_golden_set_top3_accuracy_at_least_70pct` xfail flips to hard assertion. | T7.3 | [~] | QA | P2 |
| T7.4.x | Acceptance | • **Achieve:** Replace single happy-path chaos test with partial-failure suite (C1–C6).<br>• **Deliver:** T-CHAOS track rows C1–C6 all green; this row flips `[x]` when all six are green for ≥ 3 consecutive nightly runs. | T5.6, T7.4 | [~] | SRE | P2 |

---

## Track T-CHAOS — Chaos Drill Suite (P2.6 / T7.4.x) — 2026-05-11

**Counter: 完成 8 / 未完成 0 / descope 1**

| # | Category | Task | Depends On | Status | Owner |
|---|---|---|---|:---:|---|
| T-CHAOS.0 | Structural | • **Achieve:** Establish chaos suite scaffold and pin fixture-reuse policy.<br>• **Deliver:** `tests/e2e/test_chaos/__init__.py` + `conftest.py`; `chaos_drill_outcome_total` counter in `bootstrap/metrics.py`. | T7.4 | [x] | SRE |
| T-CHAOS.C1 | Red+Green | • **Achieve:** Validate worker SIGKILL recovery.<br>• **Deliver:** `tests/e2e/test_chaos/test_C1_worker_sigkill.py`. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C2 | Red+Green | • **Achieve:** Validate worker crash between MariaDB commit and ES bulk leaves a recoverable state.<br>• **Deliver:** `tests/e2e/test_chaos/test_C2_db_es_split.py`. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C3 | Red+Green | • **Achieve:** Validate ES bulk 207 partial failure is retried idempotently.<br>• **Deliver:** `tests/e2e/test_chaos/test_C3_es_bulk_207.py`. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C4 | Red+Green | • **Achieve:** Validate rerank 5xx fail-open.<br>• **Deliver:** `tests/e2e/test_chaos/test_C4_rerank_5xx.py`. | P2.3 | [x] | SRE |
| T-CHAOS.C5 | Red+Green | • **Achieve:** Validate LLM stream interrupt emits `data: {type:"error",...}` per B6.<br>• **Deliver:** `tests/e2e/test_chaos/test_C5_llm_stream_interrupt.py`. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C6 | Red+Green | • **Achieve:** Validate MinIO transient 503 is retried (3×@2s built-in).<br>• **Deliver:** `tests/e2e/test_chaos/test_C6_minio_503.py`. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.7 | Structural | • **Achieve:** Wire nightly CI lane for chaos suite.<br>• **Deliver:** `.github/workflows/chaos-nightly.yml` + `make test-chaos`. | T-CHAOS.C1–C6 | [x] | SRE |
| T7.4.x Closure | Closure | • **Achieve:** Flip T7.4.x spillover row when all six cases green for ≥ 3 consecutive nightly runs.<br>• **Deliver:** plan.md row `T7.4.x` → `[x]` with evidence. | T-CHAOS.7 | [~] | SRE |

---

## Phase 2 — Production Quality

**Counter: 完成 5 / 未完成 0 / descope 4**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P2.1 | Stability | • **Achieve:** Production-grade HA + observability.<br>• **Deliver:** `docs/ha_runbook.md`, Grafana dashboard, alerting rules. | [x] | SRE |
| P2.2 | Security | • **Achieve:** Activate JWT + Permission layer per Track T8.<br>• **Deliver:** All `[~]` rows in T8 → `[x]`; remove `RAGENT_AUTH_DISABLED`; introduce `RAGENT_AUTH_MODE`. | [~] | Dev |
| P2.3 | Behavioral | • **Achieve:** Improve chat ranking via reranker fail-open resilience.<br>• **Deliver:** `UpstreamServiceError`/`UpstreamTimeoutError` → log `rerank.degraded` + increment `rerank_degraded_total{reason}` + return RRF-ordered docs[:top_k]. | [x] | Dev |
| P2.4 | Behavioral | • **Achieve:** Route translate/summarize intents to direct LLM, bypassing retrieval.<br>• **Deliver:** `ConditionalRouter` intent split. | [~] | Dev |
| P2.5 | Behavioral | • **Achieve:** Replace P1 501 stub with real MCP JSON-RPC 2.0 server (B47, §3.8).<br>• **Deliver:** T-MCP rows T-MCP.1–T-MCP.12 all `[x]`. | [x] | Dev |
| P2.6 | Quality | • **Achieve:** Continuous answer-quality + load resilience evidence.<br>• **Deliver:** RAGAS eval in CI; large-file streaming; chaos drills. | [~] | QA |
| P2.7 | Behavioral | • **Achieve:** Concurrent component execution for ingest/chat.<br>• **Deliver:** Switch ingest/chat to Haystack `AsyncPipeline`. | [~] | Dev |
| P2.8 | Closure | • **Achieve:** Close P2 with synced docs and lessons.<br>• **Deliver:** Updated `00_spec.md` / `00_plan.md` + new `00_journal.md` entries. | [x] | Master |
| P2.9 | Stability | • **Achieve:** Close prior MinIO orphan-sweeper idea as not-doing.<br>• **Deliver:** MinIO objects retained for audit/replay; no TTL sweeper installed. | [x] | SRE |

---

## Phase 3 — Graph Enhancement (conditional) — gated / descoped

**Counter: 完成 0 / 未完成 0 / descope 5**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P3.1 | Decision | • **Achieve:** Lock graph DB choice with a written rationale.<br>• **Deliver:** ADR for Graph DB selection. | [~] | Architect |
| P3.2 | Behavioral | • **Achieve:** Replace stub with a real graph extractor.<br>• **Deliver:** `GraphExtractor` implementation. | [~] | Dev |
| P3.3 | Behavioral | • **Achieve:** Add graph retrieval branch to chat pipeline.<br>• **Deliver:** `HybridRetrieverWithGraph` SuperComponent. | [~] | Dev |
| P3.4 | Governance | • **Achieve:** Govern entity lifecycle in the graph store.<br>• **Deliver:** Entity soft-delete + ref_count + GC + reconciliation cron. | [~] | Dev |
| P3.5 | Gate | • **Achieve:** Confirm graph track is justified before spend.<br>• **Deliver:** Gate decision: P2 stable ≥ 4 weeks AND hybrid alone underperforms on relational queries. | [~] | PM |

---

## Track T-MH — MCP Hub Microservice — 2026-05-25

**Counter: 完成 13 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MH.0 | Kickoff | • **Achieve:** Land the dynamic Hub skeleton — YAML schema, signature factory, httpx forwarder, lifespan-managed client, Streamable HTTP entry point.<br>• **Deliver:** `src/ragent/mcp_hub/{mcp_hub.py,server.py,tools.example.yaml,__init__.py}` + `tests/unit/mcp_hub/test_signature_factory.py`. | [x] | Dev |
| T-MH.1 | Spec | • **Achieve:** Document the Hub microservice in `docs/00_spec.md`. | [x] | Spec |
| T-MH.2 | Test | • **Achieve:** Add an integration test that boots the Hub against a stub upstream over Streamable HTTP. | [x] | QA |
| T-MH.3 | Hardening | • **Achieve:** Pre-compute per-tool wire dicts and connection limits; consider auth header pass-through. | [x] | Dev |
| T-MH.4 | Behavioral | • **Achieve:** Upstream-error transparency contract — structured envelopes replace blanket `raise_for_status`. | [x] | Dev |
| T-MH.5 | Behavioral | • **Achieve:** Static `tools.yaml` validator runnable in CI. | [x] | Dev |
| T-MH.6 | Behavioral | • **Achieve:** Address gemini-code-assist PR #79 review (three medium-priority findings). | [x] | Dev |
| T-MH.7 | Behavioral | • **Achieve:** Heterogeneous-upstream support — per-tool `base_url` override, per-tool `static_headers`, per-tool `forward_headers`. | [x] | Dev |
| T-MH.8a | Behavioral | • **Achieve:** Header model rework — drop `${ENV_VAR}` substitution; flip `forward_headers` schema to template strings. | [x] | Dev |
| T-MH.8b | Behavioral | • **Achieve:** Multi-system directory loading with per-system isolation. | [x] | Dev |
| T-MH.9 | Behavioral | • **Achieve:** Operator-facing structured logging via `structlog`. | [x] | Dev |
| T-MH.10 | Behavioral | • **Achieve:** Expose the project's own `POST /retrieve/v1` as an MCP tool by default. | [x] | Dev |
| T-MH.11 | Behavioral | • **Achieve:** Operability triple — per-system `verify_ssl`, Hub serves `GET /metrics`, `LoadFailure` carries structured fields. | [x] | Dev |
| T-MH.12 | Behavioral | • **Achieve:** Expose `build_mcp_app()` as a 0-arg uvicorn `--factory` entry point; update K8s api command; update docs. | [x] | Dev |

---

## Track T-CH — Chat Intent Detection + `retrieve` Flag — 2026-05-26

**Counter: 完成 14 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CH.D1 | Red+Green | • **Achieve:** `_requires_retrieve()` maps all known intents correctly.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_requires_retrieve_known_intents`. | [x] | Dev |
| T-CH.D2 | Red+Green | • **Achieve:** `_requires_retrieve()` defaults unknown labels to True (fail-safe).<br>• **Deliver:** `::test_requires_retrieve_unknown_defaults_true`. | [x] | Dev |
| T-CH.D3 | Red+Green | • **Achieve:** `_detect_intent()` returns correct label when LLM returns exact match.<br>• **Deliver:** `::test_detect_intent_known_label`. | [x] | Dev |
| T-CH.D4 | Red+Green | • **Achieve:** `_detect_intent()` falls back to QUESTION for unrecognised LLM output.<br>• **Deliver:** `::test_detect_intent_unknown_label_fallback`. | [x] | Dev |
| T-CH.D5 | Red+Green | • **Achieve:** `_detect_intent()` falls back to QUESTION on LLM exception.<br>• **Deliver:** `::test_detect_intent_exception_fallback`. | [x] | Dev |
| T-CH.D6 | Red+Green | • **Achieve:** `_detect_intent()` uses only the first word of multi-word LLM output.<br>• **Deliver:** `::test_detect_intent_multiword_uses_first_word`. | [x] | Dev |
| T-CH.R1 | Red+Green | • **Achieve:** `build_rag_messages(inject_context=False)` passes messages through without `<context>` wrapping.<br>• **Deliver:** `::test_inject_context_false_no_context_tag`. | [x] | Dev |
| T-CH.R2 | Red+Green | • **Achieve:** `build_rag_messages(inject_context=False)` still floats caller system messages to front.<br>• **Deliver:** `::test_inject_context_false_system_floated`. | [x] | Dev |
| T-CH.R3 | Red+Green | • **Achieve:** `ChatRequest.retrieve` field defaults True and accepts False.<br>• **Deliver:** `::test_chat_request_retrieve_field`. | [x] | Dev |
| T-CH.P1 | Red+Green | • **Achieve:** `_RAG_COMMON_INSTRUCTIONS` contains the GROUNDED RESPONSE OPENER rule.<br>• **Deliver:** `::test_system_prompt_contains_grounded_opener_rule`. | [x] | Dev |
| T-CH.I1 | Red+Green | • **Achieve:** `POST /chat/v1 {retrieve:false}` skips intent detection + pipeline; `sources=[]`.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_retrieve_false_skips_pipeline`. | [x] | Dev |
| T-CH.I2 | Red+Green | • **Achieve:** `POST /chat/v1/stream {retrieve:false}` done frame has `sources=[]`.<br>• **Deliver:** `::test_stream_retrieve_false_sources_empty`. | [x] | Dev |
| T-CH.I3 | Red+Green | • **Achieve:** `POST /chat/v1` with intent=GREETING skips retrieval pipeline; `sources=[]`.<br>• **Deliver:** `::test_greeting_intent_skips_retrieval`. | [x] | Dev |
| T-CH.I4 | Red+Green | • **Achieve:** `POST /chat/v1` with intent=QUESTION still runs retrieval pipeline.<br>• **Deliver:** `::test_question_intent_runs_retrieval`. | [x] | Dev |

---

## Track T-CH2 — context_mode, per-intent temperature, prompt selection — 2026-05-26

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CH2.S1 | Red+Green | • **Achieve:** `ChatRequest.context_mode` replaces `retrieve: bool`.<br>• **Deliver:** `tests/unit/test_chat_request_schema.py::test_context_mode_*`. | [x] | Dev |
| T-CH2.S2 | Red+Green | • **Achieve:** `ChatRequest.temperature` becomes `float \| None = None`.<br>• **Deliver:** `::test_temperature_none_accepted`. | [x] | Dev |
| T-CH2.S3 | Red+Green | • **Achieve:** `build_rag_messages(intent=GREETING, inject_context=False)` uses `_PLAIN_ASSISTANT_PROMPT`.<br>• **Deliver:** `::test_plain_prompt_for_greeting_no_context`. | [x] | Dev |
| T-CH2.S4 | Red+Green | • **Achieve:** `build_rag_messages(intent=QUESTION, inject_context=True)` prompt contains `[N]` citation rules.<br>• **Deliver:** `::test_rag_prompt_has_citation_when_inject_context`. | [x] | Dev |
| T-CH2.S5 | Red+Green | • **Achieve:** `build_rag_messages(intent=QUESTION, inject_context=False)` prompt has NO `[N]` citation rules.<br>• **Deliver:** `::test_no_citation_prompt_when_caller_context`. | [x] | Dev |
| T-CH2.R1 | Red+Green | • **Achieve:** `_INTENT_TEMPERATURE` maps all intents; unknown defaults to `_DEFAULT_TEMPERATURE`.<br>• **Deliver:** `::test_intent_temperature_mapping`. | [x] | Dev |
| T-CH2.R2 | Red+Green | • **Achieve:** `context_mode="caller"` always skips retrieval regardless of intent.<br>• **Deliver:** `::test_caller_mode_always_skips_retrieval`. | [x] | Dev |
| T-CH2.R3 | Red+Green | • **Achieve:** `context_mode="force"` always runs retrieval regardless of intent.<br>• **Deliver:** `::test_force_mode_always_runs_retrieval`. | [x] | Dev |
| T-CH2.R4 | Red+Green | • **Achieve:** Intent detection always runs regardless of `context_mode`.<br>• **Deliver:** `::test_intent_detection_runs_for_all_context_modes`. | [x] | Dev |
| T-CH2.I1 | Red+Green | • **Achieve:** `context_mode="caller"` + QUESTION: `sources=null`, no `<context>`, no `[N]`.<br>• **Deliver:** `::test_caller_mode_no_citation_in_prompt`. | [x] | Dev |
| T-CH2.I2 | Red+Green | • **Achieve:** `temperature=null` + GREETING: LLM called with `_INTENT_TEMPERATURE["GREETING"]`.<br>• **Deliver:** `::test_auto_temperature_greeting`. | [x] | Dev |
| T-CH2.I3 | Red+Green | • **Achieve:** `context_mode="force"` + GREETING: retrieval runs, sources populated.<br>• **Deliver:** `::test_force_mode_retrieval_runs`. | [x] | Dev |

---

## Track T-twp-ai — twp-ai Protocol Alignment — 2026-05-27

**Counter: 完成 4 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-twp-ai.1 | Red+Green | • **Achieve:** Accept twp-ai required run input fields and top-level client-provided tool definitions.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/schemas.py` + `::test_run_agent_input_accepts_twp_ai_tool_shape`. | [x] | Dev |
| T-twp-ai.2 | Red+Green | • **Achieve:** Emit twp-ai tool lifecycle events for direct LLM tool calls.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/events.py`, `agents/direct.py` + `::test_direct_agent_emits_twp_ai_tool_lifecycle_events`. | [x] | Dev |
| T-twp-ai.3 | Red+Green | • **Achieve:** Preserve two-turn confirmation by translating tool result back into provider messages. _(Superseded by T-twp-ai.4.)_<br>• **Deliver:** `packages/twp-ai/src/twp_ai/_compose.py`. | [x] | Dev |
| T-twp-ai.4 | Red+Green | • **Achieve:** Wait for client tool results — stop after tool-call events; continuation run carries `role="tool"` messages.<br>• **Deliver:** `agents/direct.py`, `_compose.py` + `::test_direct_agent_preserves_client_tool_result_history`. | [x] | Dev |

---

## Track T-AM — Auth Mode Consolidation — 2026-05-28

**Counter: 完成 5 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-AM.S1 | Structural | • **Achieve:** `AuthMode` enum + `parse_auth_mode()` as the single source of truth for mode resolution.<br>• **Deliver:** `src/ragent/bootstrap/auth_mode.py`; `tests/unit/test_auth_mode_parse.py`. | [x] | Dev |
| T-AM.1 | Behavioral | • **Achieve:** Guard enforces `RAGENT_AUTH_MODE` rules, replacing old two-bool logic.<br>• **Deliver:** Rewrite `src/ragent/bootstrap/guard.py`; `tests/unit/test_bootstrap_startup_guard.py`. | [x] | Dev |
| T-AM.2 | Behavioral | • **Achieve:** Middleware + composition handle all 4 modes; `none` injects `"anonymous"`; `jwt_prefer_header` tries JWT first.<br>• **Deliver:** Updated `app.py` middleware, `composition.py` JWT-manager guard, `openapi.py`. | [x] | Dev |
| T-AM.3 | Behavioral | • **Achieve:** `RAGENT_JWT_VERIFY_AUD` + `RAGENT_JWT_VERIFY_EXP` respected by JWT verifier.<br>• **Deliver:** `tests/unit/test_jwt_verify_flags.py`. | [x] | Dev |
| T-AM.S2 | Structural | • **Achieve:** Remove `RAGENT_AUTH_DISABLED` + `RAGENT_TRUST_X_USER_ID_HEADER` from all source, tests, and docs.<br>• **Deliver:** Updated `docs/spec/env_vars.md`, `docs/00_spec.md`, `.env.example`. | [x] | Dev |

---

## Track T-MCP2 — MCP retrieve tool input/output alignment — 2026-06-01

**Counter: 完成 3 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MCP2.1 | Behavioral | • **Achieve:** `inputSchema` is a closed schema — unknown arguments rejected with -32602.<br>• **Deliver:** `::test_tools_call_retrieve_rejects_unknown_argument`. | [x] | Dev |
| T-MCP2.2 | Behavioral | • **Achieve:** `tools/call retrieve` response `content[0].text` is `[資料來源 #N]`-formatted text.<br>• **Deliver:** `::test_tools_call_retrieve_text_format_*`. | [x] | Dev |
| T-MCP2.3 | Behavioral | • **Achieve:** Header metadata fields have CR/LF stripped to prevent injection.<br>• **Deliver:** `::test_tools_call_retrieve_sanitizes_newlines_in_header_metadata`; `_header_field()` helper. | [x] | Dev |

---

## Track T-MCP13 — MCP Structured Tool Output (protocol 2025-06-18, B63) — 2026-06-10

**Counter: 完成 9 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MCP13.1 | Behavioral | • **Achieve:** `tools/list` advertises `outputSchema` on `retrieve`.<br>• **Deliver:** `::test_retrieve_tool_advertises_output_schema`. | [x] | Dev |
| T-MCP13.2 | Behavioral | • **Achieve:** `tools/call retrieve` returns `structuredContent: {sources: [...]}`. | [x] | Dev |
| T-MCP13.3 | Behavioral | • **Achieve:** `content[0].text` is `<context>`-wrapped markdown citation table + excerpt blocks. | [x] | Dev |
| T-MCP13.4 | Behavioral | • **Achieve:** `structuredContent` validates against `outputSchema`; markdown table is injection-safe. | [x] | Dev |
| T-MCP13.5 | Behavioral | • **Achieve:** `initialize` advertises `protocolVersion: "2025-06-18"`. | [x] | Dev |
| T-MCP13.6 | Behavioral | • **Achieve:** A `\|` in `source_url` cannot split the citation-table row. | [x] | Dev |
| T-MCP13.7 | Behavioral | • **Achieve:** Only `http(s)` `source_url` values are linkified; unsafe chars percent-encoded. | [x] | Dev |
| T-MCP13.8 | Behavioral | • **Achieve:** Literal `<context>`/`</context>` tags inside titles/excerpts neutralised to `&lt;…&gt;`. | [x] | Dev |
| T-MCP13.9 | Behavioral | • **Achieve:** `initialize` negotiates the protocol revision — echoes supported version, falls back for unsupported. | [x] | Dev |

---

## Track T-CA — ChatAgent Proxy Endpoints — 2026-06-02

**Counter: 完成 12 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CA.S1 | Structural | • **Achieve:** `CHATAGENT_UPSTREAM_ERROR`, `CHATAGENT_TIMEOUT`, `CHATAGENT_RATE_LIMITED` in `HttpErrorCode`. | [x] | Dev |
| T-CA.S2 | Structural | • **Achieve:** `ChatAgentRequest(ChatRequest)` with optional `session: str \| None`. | [x] | Dev |
| T-CA.R1 | Behavioral | • **Achieve:** `POST /chatagent/v1` proxies to `CHATAGENT_API_URL`. | [x] | Dev |
| T-CA.R2 | Behavioral | • **Achieve:** `GET /chatagent/v1/sessionList` proxies; injects user/apName. | [x] | Dev |
| T-CA.R3 | Behavioral | • **Achieve:** `GET /chatagent/v1/session` proxies; injects user/apName/session. | [x] | Dev |
| T-CA.I1 | Behavioral | • **Achieve:** Routes registered conditionally by URL env var; integration tests via TestClient + mocked httpx. | [x] | Dev |
| T-CA.W1 | Behavioral | • **Achieve:** Composition root reads 5 new env vars; app.py registers router when any URL is set. | [x] | Dev |
| T-CA.D1 | Structural | • **Achieve:** All new env vars documented; API.md + third-party API doc updated. | [x] | Dev |
| T-CA.R4 | Behavioral | • **Achieve:** `POST /chatagent/v1` response body includes `session` field (supplied or auto-generated). | [x] | Dev |
| T-CA.R5 | Behavioral | • **Achieve:** `PUT /chatagent/v1/session` proxies; `SessionRenameRequest` schema. | [x] | Dev |
| T-CA.R6 | Behavioral | • **Achieve:** `DELETE /chatagent/v1/session` proxies; `SessionDeleteRequest` schema. | [x] | Dev |
| T-CA.R7 | Behavioral | • **Achieve:** `_proxy_write` handles empty/204 upstream responses without false 502. | [x] | Dev |

---

## Track T-DEL1 — VectorExtractor.delete() candidate-index alignment — 2026-06-04

**Counter: 完成 2 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-DEL1.1 | Behavioral | • **Achieve:** `VectorExtractor.delete()` fans out across all live write targets (stable + candidate).<br>• **Deliver:** `_IndexProvider` Protocol + `_delete_indices()` helper + tests. | [x] | Dev |
| T-DEL1.2 | Behavioral | • **Achieve:** Composition wires `ActiveModelRegistry` into `VectorExtractor`.<br>• **Deliver:** Reordered `composition.py`; `registry=embedding_registry` kwarg. | [x] | Dev |

---

## Track T-DEL2 — PR #149 review findings — 2026-06-04

**Counter: 完成 2 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-DEL2.1 | Behavioral | • **Achieve:** `_delete_indices()` deduplicates when `candidate == stable`.<br>• **Deliver:** `test_delete_indices_deduplicates_when_candidate_equals_stable`. | [x] | Dev |
| T-DEL2.2 | Behavioral | • **Achieve:** Reconciler warms `ActiveModelRegistry` before fan-out.<br>• **Deliver:** `await container.embedding_registry.refresh()` in `_PerTickRunner._tick()`. | [x] | Dev |

---

## Track T-CAv2 — ChatAgent v2 Raw-Proxy Endpoint — 2026-06-03

**Counter: 完成 5 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv2.S1 | Structural | • **Achieve:** Accept arbitrary JSON body (`dict[str, Any]`) — server injects `apName`/`user`/`userToken` into `metadata`.<br>• **Deliver:** `src/ragent/routers/chatagent_v2.py`. | [x] | Dev |
| T-CAv2.R1 | Behavioral | • **Achieve:** `POST /chatagent/v2` non-streaming — inject server fields, POST upstream, forward raw bytes + upstream `Content-Type`. | [x] | Dev |
| T-CAv2.R2 | Behavioral | • **Achieve:** `POST /chatagent/v2` streaming — `stream: true` uses unified `send(stream=True)` path, validates upstream status, returns `StreamingResponse`. | [x] | Dev |
| T-CAv2.R3 | Behavioral | • **Achieve:** Rate limiting with key `"chatagent:{user_id}"` → 429 `CHATAGENT_RATE_LIMITED`. | [x] | Dev |
| T-CAv2.W1 | Behavioral | • **Achieve:** Router registered in `bootstrap/app.py` under the existing `CHATAGENT_API_URL` guard. | [x] | Dev |

---

## Track T-OPS — Batch Retry Operation API — 2026-06-05

**Counter: 完成 5 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-OPS.R1 | Behavioral | • **Achieve:** `DocumentRepository.count_by_statuses()` + `list_by_statuses()` with optional filters.<br>• **Deliver:** `tests/unit/test_document_repository_ops.py` (12 tests). | [x] | Dev |
| T-OPS.R2 | Behavioral | • **Achieve:** `IngestService.batch_rerun()` — dry_run preview, per-doc mark+enqueue loop, before/after count snapshot.<br>• **Deliver:** `tests/unit/test_ingest_service_batch_rerun.py` (11 tests). | [x] | Dev |
| T-OPS.R3 | Behavioral | • **Achieve:** `POST /ops/v1/retry` endpoint with OpsRetryRequest/OpsRetryResponse schemas.<br>• **Deliver:** `src/ragent/routers/admin_ops.py`; `tests/unit/test_admin_ops_router.py` (10 tests). | [x] | Dev |
| T-OPS.W1 | Behavioral | • **Achieve:** Register admin_ops router in `bootstrap/app.py`. | [x] | Dev |
| T-OPS.R4 | Behavioral | • **Achieve:** PR review hardening — entry log, per-item dispatch log, operator_id audit field, extra-field rejection, `idx_status_created` index.<br>• **Deliver:** `migrations/012_documents_status_created_index.sql`; 16+14 tests. | [x] | Dev |

---

## Track T-MCP-REG — MCP v1 Tool Registration Best Practices — 2026-06-07

**Counter: 完成 4 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MCP-REG.1 | Behavioral | • **Achieve:** Replace hand-written `_RETRIEVE_TOOL_SCHEMA` dict with `mcp.types.Tool` descriptor; `_build_mcp_input_schema()` from Pydantic; `_ALL_TOOLS` registry.<br>• **Deliver:** `src/ragent/routers/mcp_tools/__init__.py` + `mcp_tools/retrieve.py`. | [x] | Dev |
| T-MCP-REG.2 | Behavioral | • **Achieve:** Add agent-oriented `description=` to all six fields of `RetrieveRequest`.<br>• **Deliver:** `src/ragent/schemas/retrieve.py`; `docs/spec/mcp_server.md §3.8.3`. | [x] | Dev |
| T-MCP-REG.3 | Behavioral | • **Achieve:** Fix `_build_mcp_input_schema`: strip `"default": null` after collapsing `anyOf`.<br>• **Deliver:** `::test_retrieve_optional_fields_have_no_null_default`. | [x] | Dev |
| T-MCP-REG.4 | Behavioral | • **Achieve:** Improve `retrieve` tool description UX — behavior-oriented language; remove misleading `source_app` examples.<br>• **Deliver:** `mcp_tools/retrieve.py`; `schemas/retrieve.py`; `docs/spec/mcp_server.md`. | [x] | Dev |

---

## Track T-CAv3 — ChatAgent v3 (twp-ai protocol proxy) — 2026-06-08

**Counter: 完成 8 / 未完成 0 / descope 0**

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv3.1 | Red+Green | • **Achieve:** `ADKCaller` protocol — structural `stream_deltas(request, model) -> Generator[str]`.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/callers/adk.py`. | [x] | Dev |
| T-CAv3.2 | Red+Green | • **Achieve:** `ADKAgent.run()` emits twp-ai text lifecycle from caller deltas; caller exception → `RUN_ERROR`.<br>• **Deliver:** `agents/adk.py`; `tests/test_adk_agent.py`. | [x] | Dev |
| T-CAv3.3 | Red+Green | • **Achieve:** ragent-side concrete `ADKCaller` — builds upstream payload, parses `returnData.delta`/`done`, raises typed errors.<br>• **Deliver:** `src/ragent/clients/adk_caller.py`; `tests/unit/test_adk_caller.py`. | [x] | Dev |
| T-CAv3.4 | Red+Green | • **Achieve:** `POST /chatagent/v3` — `get_user_id` dep, builds `RunAgentInput`, streams `ADKAgent` events; rate-limit → 200 SSE `RUN_ERROR`.<br>• **Deliver:** `src/ragent/routers/chatagent_v3.py`; `tests/unit/test_chatagent_v3_router.py`. | [x] | Dev |
| T-CAv3.W1 | Behavioral | • **Achieve:** Register `/chatagent/v3` in `bootstrap/app.py`.<br>• **Deliver:** `tests/integration/test_chatagent_v3_endpoint.py`. | [x] | Dev |
| T-CAv3.D1 | Structural | • **Achieve:** Document the v3 contract.<br>• **Deliver:** `docs/00_spec.md` (v3 System Interface), `docs/API.md`. | [x] | Dev |
| T-CAv3.5 | Red+Green | • **Achieve:** Map the upstream `planner` node to a reasoning block (`REASONING_START`/`CONTENT`/`END`).<br>• **Deliver:** 5 new events in `events.py`; `agents/adk.py` block-kind tracking; `docs/00_spec.md §3.4.7`. | [x] | Dev |
| T-CAv3.6 | Red+Green | • **Achieve:** Surface client-supplied `context`/`state` by prepending a labelled preamble to the last user message.<br>• **Deliver:** `clients/adk_caller.py` (`_compose_message`, `_context_preamble`); `docs/00_spec.md §3.4.7`. | [x] | Dev |
