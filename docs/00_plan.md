# 00_plan.md — Master TDD Implementation Checklist

> Source: `docs/00_spec.md` · Authored: 2026-05-03 · Reorg: 2026-05-04 (Round 3)
> Workflow: `CLAUDE.md` §THE TDD WORKFLOW · Tidy First (`[STRUCTURAL]` / `[BEHAVIORAL]`)
> Each `[ ]` = one Red→Green→Refactor cycle, each cycle = one (or two) commit.
> Reorg driver: `docs/team/2026_05_04_phase1_round3_reorg_auth_off.md` (12/12 6-of-6).

## Status legend
- `[x]` delivered
- `[ ]` TODO
- `[~]` descoped / deferred (not doing in this cycle)

---

> Phase 1 tracks (T0-T8): [docs/completed_plan/phase1_tracks.md](completed_plan/phase1_tracks.md)
> Phase 1 v2 completed tracks: [docs/completed_plan/phase1_v2_tracks.md](completed_plan/phase1_v2_tracks.md)

---

## Phase 1 → Phase 2 spillover

> Two acceptance gates promised in P1 cannot run inside the per-PR CI gate as-is — both need infrastructure (live AI endpoints / slow-job chaos lane) that is P2-owned. Recorded here so DoD §200 stays an absolute invariant for tracks T0–T7 and the spillovers stay visible.

| # | Category | Task | Depends On | Status | Owner | Phase |
|---|---|---|---|:---:|---|:---:|
| T7.3.x | Acceptance | • **Achieve:** Wire the T7.3 retrieval-recall SLO to a real automated gate against live embedder/rerank/LLM endpoints (closes the gap noted in `docs/00_journal.md::E2E gate integrity`).<br>• **Deliver:** (a) Scheduled / label-triggered CI job that exports real `EMBEDDING_API_URL` / `LLM_API_URL` / `RERANK_API_URL` + tokens via secrets, runs `make test-e2e-golden`; (b) decision-log row pinning which endpoint identity is used + cost expectation per run; (c) `tests/e2e/test_golden_set.py::test_golden_set_top3_accuracy_at_least_70pct` xfail flips to a hard assertion on that job. Default WireMock e2e remains the per-PR gate. | T7.3 | [~] | QA | P2 |
| T7.4.x | Acceptance | • **Achieve:** Replace the single happy-path chaos test (currently xfail run=False, `tests/e2e/test_chaos_worker_kill.py`) with a partial-failure suite covering the cross-storage failure modes that motivated the test.<br>• **Deliver:** decomposed into Track T-CHAOS rows C1–C6 below; this row stays for traceability and flips `[x]` when all six are green. | T5.6, T7.4 | [~] | SRE | P2 |

---

## Track T-CHAOS — Chaos Drill Suite (P2.6 軌三 / T7.4.x) — 2026-05-11

> Spec: `00_spec.md` §3.6.1 (B49). Each case under `tests/e2e/test_chaos/test_C<N>_<scenario>.py`, marked `@pytest.mark.docker`, gated by nightly CI lane (not per-PR). Common asserts per §3.6.1: terminal status; ES/DB consistency; OTEL spans; `chaos_drill_outcome_total{case,outcome}` increment.

| # | Category | Task | Depends On | Status | Owner |
|---|---|---|---|:---:|---|
| T-CHAOS.0 | Structural | • **Achieve:** Establish chaos suite scaffold and pin fixture-reuse policy.<br>• **Deliver:** `tests/e2e/test_chaos/__init__.py` + `tests/e2e/test_chaos/conftest.py` — shared fixtures: WireMock reset between cases, `chaos_drill_outcome_total` metric scrape helper. **Fixture-reuse policy** (avoid 6× ~30s testcontainer boot tax): C3/C4/C5/C6 (WireMock-only injection) reuse session-scoped `running_stack` from `tests/e2e/conftest.py`; C1/C2 (worker kill / split-brain) use function-scope `spawn_module` because they kill the worker process. `chaos_drill_outcome_total` counter added to `src/ragent/bootstrap/metrics.py` with labels `case`, `outcome`. | T7.4 | [x] | SRE |
| T-CHAOS.C1 | Red+Green | • **Achieve:** Validate worker SIGKILL recovery (existing test unblocked).<br>• **Deliver:** Move `tests/e2e/test_chaos_worker_kill.py` → `tests/e2e/test_chaos/test_C1_worker_sigkill.py` ✓; lift `xfail(run=False)` and write the four `@spec §3.6.1` assertions ✓; xfail(strict=True) lifted on 2026-05-14 after `_claim` was rewritten as `_atomic_claim`. See journal SRE 2026-05-14 "Recovery semantics closeout". | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C2 | Red+Green | • **Achieve:** Validate worker crash between MariaDB commit and ES bulk leaves a recoverable state.<br>• **Deliver:** `tests/e2e/test_chaos/test_C2_db_es_split.py` — monkeypatch worker `_commit_ready` to raise `ConnectionError` post-DB-commit, pre-ES-bulk; restart worker; assert reconciler heals via R3 multi-READY-repair or worker retry; final state READY with ES chunks present. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C3 | Red+Green | • **Achieve:** Validate ES bulk 207 partial failure is retried idempotently.<br>• **Deliver:** `tests/e2e/test_chaos/test_C3_es_bulk_207.py` — `DocumentEmbedder._run_dual` checks `errors:true` in bulk response, logs `es.bulk_partial_failure` per failed item, retries only failed items in a second bulk call. Mock-based in-process test; `call_count == 2` confirmed. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C4 | Red+Green | • **Achieve:** Validate rerank 5xx fail-open (depends on P2.3 reranker wired).<br>• **Deliver:** `tests/e2e/test_chaos/test_C4_rerank_5xx.py` — WireMock `/rerank` returns 500 for 3 consecutive calls; chat returns 200 with RRF-ordered sources; `rerank_degraded_total{reason="5xx"}+=3`. Blocked on P2.3. | P2.3 | [x] | SRE |
| T-CHAOS.C5 | Red+Green | • **Achieve:** Validate LLM stream interrupt emits `data: {type:"error",...}` per B6.<br>• **Deliver:** `tests/e2e/test_chaos/test_C5_llm_stream_interrupt.py` — `LLMStreamInterruptedError` added; `_do_stream` tracks `seen_done`; raises on EOF without `[DONE]`; `stream()` never retries on interrupt; chat router reads `error_code` dynamically from exc. WireMock injects 3 deltas no `[DONE]`; last SSE frame `error_code==LLM_STREAM_INTERRUPTED`. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.C6 | Red+Green | • **Achieve:** Validate MinIO transient 503 is retried (3×@2s built-in).<br>• **Deliver:** `tests/e2e/test_chaos/test_C6_minio_503.py` — `get_object` retries `ConnectionError`/`S3Error` transients up to `MINIO_GET_RETRIES` (default 3); logs `minio.transient_error` on each retry; re-raises client errors (`NoSuchKey`/`AccessDenied`) immediately. Mock test verifies 2 warnings + 3 call attempts. | T-CHAOS.0 | [x] | SRE |
| T-CHAOS.7 | Structural | • **Achieve:** Wire nightly CI lane for chaos suite.<br>• **Deliver:** `.github/workflows/chaos-nightly.yml` runs `pytest tests/e2e/test_chaos -m docker` on a `cron: '0 3 * * *'` schedule (03:00 UTC); `Makefile` target `make test-chaos`; nightly artefact retains test logs for 30 days via `actions/upload-artifact`. | T-CHAOS.C1–C6 | [x] | SRE |
| T7.4.x | Closure | • **Achieve:** Flip the spillover row when all six cases green for ≥ 3 consecutive nightly runs.<br>• **Deliver:** plan.md row `T7.4.x` → `[x]` with evidence (nightly run links). | T-CHAOS.7 | [~] | SRE |

---

## Track TA — aiomysql Adoption — 100% complete → see [docs/completed_plan/phase1_v2_tracks.md](completed_plan/phase1_v2_tracks.md)

---

## Phase 2 — Production Quality (+3 weeks) — *complete (delivered items [x]; descoped items [~])*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P2.1 | Stability | • **Achieve:** Production-grade HA + observability.<br>• **Deliver:** SRE HA verification report (`docs/ha_runbook.md`), Grafana dashboard (`deploy/grafana/ragent_overview.json`), alerting rules (`deploy/prometheus/alerts.yaml` — 4 new alerts). | [x] | SRE |
| P2.2 | Security | • **Achieve:** Activate JWT + Permission layer per Track T8; B14 invariant (ES carries no auth fields).<br>• **Deliver:** All `[~]` rows in T8 → `[ ]` → `[x]`; remove `RAGENT_AUTH_DISABLED`; introduce `RAGENT_TRUST_X_USER_ID_HEADER` (default `false`) and per-surface `RAGENT_PERMISSION_INGEST_ENABLED` / `RAGENT_PERMISSION_CHAT_ENABLED` (both default `false`) — wiring lands but enforcement stays opt-in. | [~] | Dev |
| P2.3 | Behavioral | • **Achieve:** Improve chat ranking via reranker.<br>• **Deliver:** `RerankClient` wired into chat pipeline as `HybridRetrieverWithRerank` SuperComponent. Reranker wiring was completed in P1; P2.3 delivers fail-open resilience: `UpstreamServiceError` / `UpstreamTimeoutError` → log `rerank.degraded` + increment `rerank_degraded_total{reason}` + return RRF-ordered docs[:top_k]. | [x] | Dev |
| P2.4 | Behavioral | • **Achieve:** Route translate/summarize intents to direct LLM, bypassing retrieval.<br>• **Deliver:** `ConditionalRouter` intent split. | [~] | Dev |
| P2.5 | Behavioral | • **Achieve:** Replace P1 501 stub with real MCP JSON-RPC 2.0 server exposing the `retrieve` tool (B47, §3.8).<br>• **Deliver:** decomposed into Track T-MCP rows; flips `[x]` when T-MCP.1–T-MCP.12 are all `[x]`. | [x] | Dev |
| P2.6 | Quality | • **Achieve:** Continuous answer-quality + load resilience evidence.<br>• **Deliver:** RAGAS eval in CI; large-file streaming; chaos drills (軌三 decomposed into Track T-CHAOS, B49). | [~] | QA |
| P2.7 | Behavioral | • **Achieve:** Concurrent component execution for ingest/chat.<br>• **Deliver:** Switch ingest/chat to Haystack `AsyncPipeline`. | [~] | Dev |
| P2.8 | Closure | • **Achieve:** Close P2 with synced docs and lessons.<br>• **Deliver:** Updated `00_spec.md` (C4 pinned, alert table extended, P2.7 deferred note, §3.4 P-A ref removed) / `00_plan.md` (descope pass) + new entries in `00_journal.md` (P2.3 reranker fail-open design, P2/P3 descope audit). | [x] | Master |
| P2.9 | Stability | • **Achieve:** Close prior MinIO orphan-sweeper idea as not-doing.<br>• **Deliver:** MinIO objects are retained for audit/replay; no TTL sweeper is installed. | [x] | SRE |

## Phase 3 — Graph Enhancement (conditional, +4–6 weeks) — *gated*

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| P3.1 | Decision | • **Achieve:** Lock graph DB choice with a written rationale.<br>• **Deliver:** ADR for Graph DB selection (Neo4j Community / ArcadeDB / Memgraph). | [~] | Architect |
| P3.2 | Behavioral | • **Achieve:** Replace stub with a real graph extractor on the same Protocol.<br>• **Deliver:** `GraphExtractor` implementation replacing `StubGraphExtractor`. | [~] | Dev |
| P3.3 | Behavioral | • **Achieve:** Add graph retrieval branch to chat pipeline.<br>• **Deliver:** `HybridRetrieverWithGraph` SuperComponent + `LightRAGRetriever` (200 ms TO → []). | [~] | Dev |
| P3.4 | Governance | • **Achieve:** Govern entity lifecycle in the graph store.<br>• **Deliver:** Entity soft-delete + ref_count + GC + reconciliation cron. | [~] | Dev |
| P3.5 | Gate | • **Achieve:** Confirm graph track is justified before spend.<br>• **Deliver:** Gate decision: P2 stable ≥ 4 weeks AND hybrid alone underperforms on relational queries. | [~] | PM |

---

## Completed P1 v2 sub-tracks summary

> All rows [x]. Full task text in [docs/completed_plan/phase1_v2_tracks.md](completed_plan/phase1_v2_tracks.md).

- **T-MCP** (T-MCP.1–T-MCP.12) — MCP JSON-RPC 2.0 Server — 100% complete
- **T2v** (T2v.20–T2v.45) — Phase 1 v2 Ingest API refactor — 100% complete
- **T-SR** (T-SR.1–T-SR.7) — Source-id review follow-up — 100% complete
- **T-ICU.1–T-ICU.3** — ICU analyzer convergence (T-ICU.4 below) — 100% complete
- **T-RR** (T-RR.1–T-RR.18) — Reconciler-as-safety-net follow-up — 100% complete
- **T-EF** (T-EF.1–T-EF.5), **T-AV.1** — Retrieve/Ingest enhancements + versioning — 100% complete
- **T-BL** (T-BL.1–T-BL.12) — Binary Document Loaders (DOCX/PPTX) — 100% complete
- **T-FIL** (T-FIL.1–T-FIL.6) — Ingest pipeline bug fixes — 100% complete
- **T-UP** (T-UP.1–T-UP.3) — Unprotect API integration — 100% complete
- **T-PDF** (T-PDF.1–T-PDF.5) — PDF ingest support — 100% complete
- **T-RERUN** (T-RERUN.1–T-RERUN.3) — Manual rerun endpoint — 100% complete
- **T-HTTPLOG.1–T-HTTPLOG.3** — HTTP upstream error logging — 100% complete
- **T-SEC** (T-SEC.1–T-SEC.8) — Security file-upload checks — 100% complete
- **T-OCR** (T-OCR.1–T-OCR.4) — Replace Tesseract with RapidOCR — 100% complete
- **T-HDR** (T-HDR.1–T-HDR.2) — Header/footer exclusion — 100% complete
- **T-EM** (T-EM.0–T-EM.21) + **T-EM-R** (T-EM-R.1–T-EM-R.10) — Embedding-model lifecycle — 100% complete
- **T-FB** (T-FB.1–T-FB.12) — Feedback retrieval signal — 100% complete
- **T-IUP** (T-IUP.1–T-IUP.2) — Ingest upload discriminator fix — 100% complete
- **T-EI.1, T-EI.2a, T-EI.3–T-EI.6** — ES chunks index config (T-EI.2 below) — partial
- **T-APL** (T-APL.1–T-APL.11) — API pipeline param sanity & observability — 100% complete
- **T-UP.4–T-UP.5** — Inline ingest unprotect fix — 100% complete

---

## Open items in partially-complete tracks

### ICU analyzer convergence — T-ICU.4 (remaining)

| # | Category | Task | Commit | Status | Owner |
|---|---|---|:-:|:-:|---|
| T-ICU.4 | Acceptance | • **Achieve:** Manual / staging smoke test for CJK BM25 (S36 coverage gap from B42).<br>• **Deliver:** Documented procedure: operator runs `Dockerfile.es-test` ES, applies prod mapping, indexes a `"產品規格"` doc, verifies `_analyze` tokenises into `["產品", "規格"]` and BM25 query recalls. Tracked as a release-gate manual step; not blocking pre-commit. | T-ICU.3 | [ ] | Ops |

---

### HTTP upstream error logging — T-HTTPLOG.3 (remaining)

| # | Category | Task | Spec | Status | Owner |
|---|---|---|:-:|:-:|---|
| T-HTTPLOG.3 | Refactor | • **Achieve:** /simplify + /review pass; journal-add row in `docs/00_journal.md` (Spec) recording the deliberate `http_request_payload` / `http_response_payload` denylist carve-out for upstream-error diagnostics. | §4.6.8 | [x] | Dev |

---

### MCP Hub microservice — T-MH (100% complete)

> Source: user kickoff. Standalone FastMCP service that loads `tools.yaml` at startup and dynamically registers each REST endpoint as an MCP Tool. Streamable HTTP transport.

| # | Category | Task | Spec | Status | Owner |
|---|---|---|:-:|:-:|---|
| T-MH.0 | Kickoff | • **Achieve:** Land the dynamic Hub skeleton — YAML schema, signature factory, httpx forwarder, lifespan-managed client, Streamable HTTP entry point.<br>• **Deliver:** `src/ragent/mcp_hub/{mcp_hub.py,server.py,tools.example.yaml,__init__.py}` + `tests/unit/mcp_hub/test_signature_factory.py`. | — | [x] | Dev |
| T-MH.1 | Spec | • **Achieve:** Document the Hub microservice in `docs/00_spec.md` — tools.yaml schema, env-var inventory, Streamable HTTP endpoint contract, deployment topology. | — | [x] | Spec |
| T-MH.2 | Test | • **Achieve:** Add an integration test that boots the Hub against a stub upstream and exercises one tool call over Streamable HTTP via a FastMCP client. | — | [x] | QA |
| T-MH.3 | Hardening | • **Achieve:** Pre-compute per-tool wire dicts (header-name kebab map, partitioned param lists) and connection limits from `defaults`; consider auth header pass-through.<br>• **Deliver:** updates to `src/ragent/mcp_hub/mcp_hub.py`. | — | [x] | Dev |
| T-MH.4 | Behavioral | • **Achieve:** Upstream-error transparency contract — replace blanket `raise_for_status` with structured envelopes. | — | [x] | Dev |
| T-MH.5 | Behavioral | • **Achieve:** Static `tools.yaml` validator runnable in CI. | — | [x] | Dev |
| T-MH.6 | Behavioral | • **Achieve:** Address gemini-code-assist PR #79 review (three medium-priority findings). | — | [x] | Dev |
| T-MH.7 | Behavioral | • **Achieve:** Heterogeneous-upstream support — per-tool `base_url` override, per-tool `static_headers`, per-tool `forward_headers`. | — | [x] | Dev |
| T-MH.8a | Behavioral | • **Achieve:** Header model rework — drop `${ENV_VAR}` substitution; flip `forward_headers` schema to template strings. | — | [x] | Dev |
| T-MH.8b | Behavioral | • **Achieve:** Multi-system directory loading with per-system isolation. | — | [x] | Dev |
| T-MH.9 | Behavioral | • **Achieve:** Operator-facing structured logging via `structlog`. | — | [x] | Dev |
| T-MH.10 | Behavioral | • **Achieve:** Expose the project's own `POST /retrieve/v1` as an MCP tool by default. | — | [x] | Dev |
| T-MH.11 | Behavioral | • **Achieve:** Operability triple — per-system `verify_ssl`, Hub serves `GET /metrics`, `LoadFailure` carries structured fields. | — | [x] | Dev |

---

### ES chunks index config — T-EI.2 (remaining)

> Tidy First: T-EI.1 (Structural) landed first. T-EI.2/3/4 (Behavioral) follow as second commit. T-EI.5 consolidates into Behavioral commit.

| Task | Type | Deliverables | Dep | Status | Owner |
|---|---|---|---|---|---|
| T-EI.2 | Red | • **Achieve:** Pin (a) ingest pipeline resource drift, (b) `indexed_at` mapping presence, (c) `default_pipeline` index setting.<br>• **Deliver:** `tests/integration/test_es_resource_drift.py` extended to load `resources/es/pipelines/chunks_default.json` and assert single `set` processor on `_ingest.timestamp → indexed_at`; `tests/unit/test_init_schema.py` extended to assert `chunks_v1.json` has `mappings.properties.indexed_at == {"type":"date"}` and `settings.index.default_pipeline == "chunks_default"`. `tests/integration/test_bootstrap_auto_init.py` extended: `GET _ingest/pipeline/chunks_default` returns 200 AND was created before the index. Tests MUST fail before T-EI.3. | — | [x] | QA |
