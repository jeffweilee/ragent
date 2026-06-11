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
- **T-EI (T-EI.1–T-EI.2 + T-EI.2a + T-EI.3–T-EI.6)** — ES chunks index config — 100% complete
- **T-APL** (T-APL.1–T-APL.11) — API pipeline param sanity & observability — 100% complete
- **T-UP.4–T-UP.5** — Inline ingest unprotect fix — 100% complete

---

## Open items in partially-complete tracks

### ES embedding field name clarification — T-EF-CLEAN (complete)

| # | Category | Task | Spec | Status | Owner |
|---|---|---|:-:|:-:|---|
| T-EF-CLEAN.1 | Structural | • **Achieve:** Fix misleading `_QueryEmbedder` docstring that claimed registry mode targets `embedding_<m>_<d>` fields. Correct to: always emits `embedding_field="embedding"`; alias flip handles lifecycle cutover (B61). | B61 | [x] | Dev |
| T-EF-CLEAN.2 | Structural | • **Achieve:** Remove dead `_REGISTRY_MODEL_FIELD` constant and the `PUT /_mapping` block from `tests/integration/test_chat_pipeline_retrieval.py::es_store`. That setup installed `embedding_testmodel_1024` on `chunks_v1` under the abandoned field-per-model design (B50); it was never queried because `_QueryEmbedder` hardcodes `"embedding"`. Update `_stub_registry` docstring to reflect the correct index-per-model design. | B61 | [x] | Dev |
| T-EF-CLEAN.3 | Structural | • **Achieve:** Document index-per-model design supersession of B50 in `docs/spec/decision_log.md` (B61). Also backfill missing B60 entry (`ES_CHUNKS_INDEX` overridability). Update `docs/00_spec.md` §7 range marker to B1–B61. | B61 / B60 | [x] | Dev |

---

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
| T-MH.12 | Behavioral | • **Achieve:** Expose `build_mcp_app()` as a 0-arg uvicorn `--factory` entry point; refactor `main()` to delegate to it; extend `bool_env()` to accept `"on"` sentinel; update K8s api command to uvicorn CLI; update docs. | — | [x] | Dev |

---

## Track T-CH — Chat Intent Detection + `retrieve` Flag

> Source: 2026-05-26 feature request.
> Adds LLM-based intent classification before retrieval and an explicit `retrieve` flag.
> Intent → `requires_retrieve` mapping lives in `src/ragent/routers/chat.py`; system prompt gains a "根據資料" opener rule for retrieval-grounded intents.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CH.D1 | Red+Green | • **Achieve:** `_requires_retrieve()` maps all known intents correctly.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_requires_retrieve_known_intents` — GREETING/CHITCHAT → False; QUESTION/SUMMARY/GENERATION → True. | [x] | Dev |
| T-CH.D2 | Red+Green | • **Achieve:** `_requires_retrieve()` defaults unknown labels to True (fail-safe).<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_requires_retrieve_unknown_defaults_true`. | [x] | Dev |
| T-CH.D3 | Red+Green | • **Achieve:** `_detect_intent()` returns correct label when LLM returns exact match.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_detect_intent_known_label` — mock LLM returns "GREETING" → "GREETING". | [x] | Dev |
| T-CH.D4 | Red+Green | • **Achieve:** `_detect_intent()` falls back to QUESTION for unrecognised LLM output.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_detect_intent_unknown_label_fallback`. | [x] | Dev |
| T-CH.D5 | Red+Green | • **Achieve:** `_detect_intent()` falls back to QUESTION on LLM exception.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_detect_intent_exception_fallback`. | [x] | Dev |
| T-CH.D6 | Red+Green | • **Achieve:** `_detect_intent()` uses only the first word of multi-word LLM output.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_detect_intent_multiword_uses_first_word`. | [x] | Dev |
| T-CH.R1 | Red+Green | • **Achieve:** `build_rag_messages(inject_context=False)` passes messages through without `<context>` wrapping.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_inject_context_false_no_context_tag` — system prompt still prepended. | [x] | Dev |
| T-CH.R2 | Red+Green | • **Achieve:** `build_rag_messages(inject_context=False)` still floats caller system messages to front.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_inject_context_false_system_floated`. | [x] | Dev |
| T-CH.R3 | Red+Green | • **Achieve:** `ChatRequest.retrieve` field defaults True and accepts False.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_chat_request_retrieve_field`. | [x] | Dev |
| T-CH.P1 | Red+Green | • **Achieve:** `_RAG_COMMON_INSTRUCTIONS` contains the GROUNDED RESPONSE OPENER rule.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_system_prompt_contains_grounded_opener_rule`. | [x] | Dev |
| T-CH.I1 | Red+Green | • **Achieve:** `POST /chat/v1 {retrieve:false}` skips intent detection + pipeline; `sources=[]`.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_retrieve_false_skips_pipeline`. | [x] | Dev |
| T-CH.I2 | Red+Green | • **Achieve:** `POST /chat/v1/stream {retrieve:false}` done frame has `sources=[]`.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_stream_retrieve_false_sources_empty`. | [x] | Dev |
| T-CH.I3 | Red+Green | • **Achieve:** `POST /chat/v1` with intent=GREETING skips retrieval pipeline; `sources=[]`.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_greeting_intent_skips_retrieval`. | [x] | Dev |
| T-CH.I4 | Red+Green | • **Achieve:** `POST /chat/v1` with intent=QUESTION still runs retrieval pipeline.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_question_intent_runs_retrieval`. | [x] | Dev |

---

## Track T-CH2 — context_mode, per-intent temperature, prompt selection by intent

> Source: 2026-05-26 design session (follows T-CH).
> Replaces `retrieve: bool` with `context_mode: Literal["auto","caller","force"]`;
> adds per-intent temperature (`_INTENT_TEMPERATURE`); decouples citation rules from system
> prompt so `[N]` references only appear when the system injected the context.

**Design matrix** (`context_mode` × `intent`):

| context_mode | intent | retrieve | inject_context | prompt | sources |
|---|---|---|---|---|---|
| auto | GREETING/CHITCHAT | skip | False | _PLAIN_ASSISTANT | null |
| auto | QUESTION/SUMMARY/GENERATION | run | True | _DEFAULT_RAG (with [N]) | []/[{...}] |
| caller | GREETING/CHITCHAT | skip | False | _PLAIN_ASSISTANT | null |
| caller | QUESTION/SUMMARY/GENERATION | skip | False | _RAG_NO_CITATION (no [N]) | null |
| force | any intent | run | True | _DEFAULT_RAG (with [N]) | []/[{...}] |

**Temperature** (intent-based, used when `body.temperature is None`):
`GREETING/CHITCHAT → 0.8`, `QUESTION/SUMMARY → 0.2`, `GENERATION → 0.7`

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CH2.S1 | Red+Green | • **Achieve:** `ChatRequest.context_mode` replaces `retrieve: bool`.<br>• **Deliver:** `tests/unit/test_chat_request_schema.py::test_context_mode_*` — defaults "auto", accepts "caller"/"force", rejects invalid; update T-CH.R3 test. | [x] | Dev |
| T-CH2.S2 | Red+Green | • **Achieve:** `ChatRequest.temperature` becomes `float \| None = None` (None = use intent-based auto).<br>• **Deliver:** `tests/unit/test_chat_request_schema.py::test_temperature_none_accepted`. | [x] | Dev |
| T-CH2.S3 | Red+Green | • **Achieve:** `build_rag_messages(intent=GREETING, inject_context=False)` uses `_PLAIN_ASSISTANT_PROMPT`.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_plain_prompt_for_greeting_no_context`. | [x] | Dev |
| T-CH2.S4 | Red+Green | • **Achieve:** `build_rag_messages(intent=QUESTION, inject_context=True)` prompt contains `[N]` citation rules.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_rag_prompt_has_citation_when_inject_context`. | [x] | Dev |
| T-CH2.S5 | Red+Green | • **Achieve:** `build_rag_messages(intent=QUESTION, inject_context=False)` prompt has NO `[N]` citation rules.<br>• **Deliver:** `tests/unit/test_build_rag_messages.py::test_no_citation_prompt_when_caller_context`. | [x] | Dev |
| T-CH2.R1 | Red+Green | • **Achieve:** `_INTENT_TEMPERATURE` maps all intents; unknown defaults to `_DEFAULT_TEMPERATURE`.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_intent_temperature_mapping`. | [x] | Dev |
| T-CH2.R2 | Red+Green | • **Achieve:** `context_mode="caller"` always skips retrieval regardless of intent.<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_caller_mode_always_skips_retrieval`. | [x] | Dev |
| T-CH2.R3 | Red+Green | • **Achieve:** `context_mode="force"` always runs retrieval regardless of intent (even GREETING).<br>• **Deliver:** `tests/unit/test_chat_intent.py::test_force_mode_always_runs_retrieval`. | [x] | Dev |
| T-CH2.R4 | Red+Green | • **Achieve:** Intent detection always runs regardless of `context_mode`.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_intent_detection_runs_for_all_context_modes`. | [x] | Dev |
| T-CH2.I1 | Red+Green | • **Achieve:** `context_mode="caller"` + QUESTION intent: `sources=null`, no `<context>` injection, no `[N]` in outgoing system prompt.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_caller_mode_no_citation_in_prompt`. | [x] | Dev |
| T-CH2.I2 | Red+Green | • **Achieve:** `temperature=null` + GREETING intent: LLM called with `_INTENT_TEMPERATURE["GREETING"]`.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_auto_temperature_greeting`. | [x] | Dev |
| T-CH2.I3 | Red+Green | • **Achieve:** `context_mode="force"` + GREETING intent: retrieval runs, sources populated.<br>• **Deliver:** `tests/integration/test_chat_endpoint.py::test_force_mode_retrieval_runs`. | [x] | Dev |

## Track T-twp-ai — twp-ai Protocol Alignment

> Source: 2026-05-30 design session. Align `packages/twp-ai` with twp-ai protocol
> run input and event lifecycle while keeping the direct LLM runtime minimal.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-twp-ai.1 | Red+Green | • **Achieve:** Accept twp-ai required run input fields and top-level client-provided tool definitions.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/schemas.py` + `packages/twp-ai/tests/test_twp_protocol.py::test_run_agent_input_accepts_twp_ai_tool_shape`. | [x] | Dev |
| T-twp-ai.2 | Red+Green | • **Achieve:** Emit twp-ai tool lifecycle events for direct LLM tool calls.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/events.py`, `packages/twp-ai/src/twp_ai/agents/direct.py`, and `packages/twp-ai/tests/test_twp_protocol.py::test_direct_agent_emits_twp_ai_tool_lifecycle_events`. | [x] | Dev |
| T-twp-ai.3 | Red+Green | • **Achieve:** Preserve the current two-turn direct LLM confirmation by translating the tool result back into provider-compatible messages. _(Superseded by T-twp-ai.4 — the synthetic confirmation turn was replaced by real client tool-result history.)_<br>• **Deliver:** `packages/twp-ai/src/twp_ai/_compose.py`. | [x] | Dev |
| T-twp-ai.4 | Red+Green | • **Achieve:** Wait for client tool results — the direct runtime stops after the tool-call lifecycle events (no synthetic `TOOL_CALL_RESULT`, no confirmation turn); a continuation run carrying `role="tool"` messages preserves the real client tool-result history into provider-compatible messages.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/agents/direct.py`, `packages/twp-ai/src/twp_ai/_compose.py` + `packages/twp-ai/tests/test_twp_protocol.py::test_direct_agent_preserves_client_tool_result_history`. | [x] | Dev |

### Post-merge fix — PR #130 review (2026-05-27)

| # | Category | Fix |
|---|----------|-----|
| PR130.F1 | Defensive | `result.get("content") or ""` in sync handler — guard against null/missing LLM content field (KeyError on safety-filtered responses). Gemini review comment. |

---

## Track T-AM — Auth Mode Consolidation

> Replace two-boolean auth config (`RAGENT_AUTH_DISABLED` + `RAGENT_TRUST_X_USER_ID_HEADER`) with a single `RAGENT_AUTH_MODE` enum.
> New modes: `none` (no header required, `create_user="anonymous"`) and `jwt_prefer_header` (JWT fallback to `X-User-Id`).
> New JWT verification flags: `RAGENT_JWT_VERIFY_AUD` + `RAGENT_JWT_VERIFY_EXP`.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-AM.S1 | Structural | • **Achieve:** `AuthMode` enum + `parse_auth_mode()` as the single source of truth for mode resolution.<br>• **Deliver:** `src/ragent/bootstrap/auth_mode.py` — `AuthMode(str, Enum)` with values `none \| user_header \| jwt_header \| jwt_prefer_header`; `parse_auth_mode()` reads `RAGENT_AUTH_MODE` (default `user_header`); raises `ValueError` on unknown value. Tests: `tests/unit/test_auth_mode_parse.py`. | [x] | Dev |
| T-AM.1 | Behavioral | • **Achieve:** Guard enforces `RAGENT_AUTH_MODE` rules, replacing old two-bool logic.<br>• **Deliver:** Rewrite `src/ragent/bootstrap/guard.py`; rewrite `tests/unit/test_bootstrap_startup_guard.py` — `none`/`user_header`/`jwt_prefer_header` → `dev` only; `jwt_header` → no env restriction; `jwt_header`/`jwt_prefer_header` → require `OIDC_DOMAIN` + `OIDC_AUDIENCE`. | [x] | Dev |
| T-AM.2 | Behavioral | • **Achieve:** Middleware + composition handle all 4 modes; `none` injects `"anonymous"`, skips header check; `jwt_prefer_header` tries JWT first, falls back to `X-User-Id`.<br>• **Deliver:** Update `app.py` middleware, `composition.py` JWT-manager guard, `openapi.py` `is_trust_header_mode()`. Tests: extend `tests/unit/test_bootstrap_app_middleware.py` (or create). | [x] | Dev |
| T-AM.3 | Behavioral | • **Achieve:** `RAGENT_JWT_VERIFY_AUD` (default `true`) + `RAGENT_JWT_VERIFY_EXP` (default `true`) respected by JWT verifier; both `false` require `RAGENT_ENV=dev`.<br>• **Deliver:** Guard checks flags; `build_token_manager()` receives `verify_aud` + `verify_exp`; joserfc claims options updated. Tests: `tests/unit/test_jwt_verify_flags.py`. | [x] | Dev |
| T-AM.S2 | Structural | • **Achieve:** Remove `RAGENT_AUTH_DISABLED` + `RAGENT_TRUST_X_USER_ID_HEADER` from all source, tests, and docs.<br>• **Deliver:** Delete dead reads in `guard.py`, `app.py`, `composition.py`, `openapi.py`; update `docs/spec/env_vars.md`, `docs/00_spec.md`, `.env.example` if present. | [x] | Dev |

---

## Track T-MCP2 — MCP retrieve tool input/output alignment

> Source: 2026-06-01 review session.
> Two improvements to `POST /mcp/v1` `retrieve` tool:
> (1) `inputSchema` hardening — `additionalProperties:false` + richer field descriptions so MCP hosts and agents have an accurate closed schema.
> (2) Response text aligned with `_render_context()` convention — `[資料來源 #N]` + `---` format with metadata header so calling agents can cite chunks without a second `json.loads`.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MCP2.1 | Behavioral | • **Achieve:** `inputSchema` is a closed schema — unknown arguments are rejected with -32602.<br>• **Deliver:** `tests/unit/test_mcp_tools_call_retrieve.py::test_tools_call_retrieve_rejects_unknown_argument` — extra field → -32602 `MCP_TOOL_INPUT_INVALID`. Add `additionalProperties:false` + improve field descriptions. | [x] | Dev |
| T-MCP2.2 | Behavioral | • **Achieve:** `tools/call retrieve` response `content[0].text` is `[資料來源 #N]`-formatted text, not a JSON blob.<br>• **Deliver:** `tests/unit/test_mcp_tools_call_retrieve.py::test_tools_call_retrieve_text_format_*` (numbered sources, metadata header, empty result, excerpt truncation). Update existing JSON-parse tests to match new format. | [x] | Dev |
| T-MCP2.3 | Behavioral | • **Achieve:** Header metadata fields (source_app, document_id, source_title) have CR/LF stripped to prevent injection of fake `[資料來源 #N]` header lines.<br>• **Deliver:** `tests/unit/test_mcp_tools_call_retrieve.py::test_tools_call_retrieve_sanitizes_newlines_in_header_metadata`; `_header_field()` helper in `routers/mcp.py`; integration test contract updated to `[資料來源 #N]` text format. | [x] | Dev |


## Track T-MCP13 — MCP structured tool output (protocol 2025-06-18, B63)

> Source: 2026-06-10 user request.
> The `[資料來源 #N]` text-only result leaked verbatim into calling agents' replies, and the
> frontend had no machine-readable channel for a retrieved-sources panel. Adopt MCP 2025-06-18
> structured tool output: declared `outputSchema` + `structuredContent.sources` (full entries
> for the UI) alongside a `<context>`-wrapped markdown citation table + excerpt blocks in
> `content[0].text` (user-presentable, no internal fields, no natural-language wording).

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MCP13.1 | Behavioral | • **Achieve:** `tools/list` advertises `outputSchema` on `retrieve` declaring the `structuredContent.sources` contract.<br>• **Deliver:** `tests/unit/test_mcp_tools_list.py::test_retrieve_tool_advertises_output_schema`; `RETRIEVE_OUTPUT_SCHEMA` in `routers/mcp_tools/retrieve.py` passed as `outputSchema=` to the Tool descriptor. | [x] | Dev |
| T-MCP13.2 | Behavioral | • **Achieve:** `tools/call retrieve` returns `structuredContent: {sources: [...]}` with the full 10-field entries (`doc_to_source_entry()` output); empty result ⇒ `sources: []`.<br>• **Deliver:** `tests/unit/test_mcp_tools_call_retrieve.py::test_tools_call_retrieve_structured_content_sources` + `::test_tools_call_retrieve_empty_result`; `_handle_tools_call` in `routers/mcp.py`. | [x] | Dev |
| T-MCP13.3 | Behavioral | • **Achieve:** `content[0].text` is a `<context>`-wrapped markdown citation table (`#`/`資料來源`/`來源系統`, title linked to `source_url`, `(未命名)` fallback) + `### [N]` blockquoted excerpt blocks; no internal fields (id/score/mime/source_meta) and no natural-language wording; empty result ⇒ `<context>\n</context>`.<br>• **Deliver:** `::test_tools_call_retrieve_text_is_context_wrapped_citation_table`, `::test_tools_call_retrieve_text_hides_internal_fields`, `::test_tools_call_retrieve_text_format_null_metadata`; `_render_context_markdown()` replaces `_render_chunks()` in `routers/mcp.py`. | [x] | Dev |
| T-MCP13.4 | Behavioral | • **Achieve:** `structuredContent` validates against the advertised `outputSchema`; markdown table/headings are injection-safe (CR/LF stripped, `\|` escaped) while raw values survive in `structuredContent`.<br>• **Deliver:** `::test_tools_call_retrieve_structured_content_matches_output_schema`, `::test_tools_call_retrieve_sanitizes_markdown_in_text`; `_md_cell()` helper in `routers/mcp.py`. | [x] | Dev |
| T-MCP13.5 | Behavioral | • **Achieve:** `initialize` advertises `protocolVersion: "2025-06-18"` (first revision with structured tool output).<br>• **Deliver:** `tests/unit/test_mcp_initialize.py` pin updated; `_MCP_PROTOCOL_VERSION` in `routers/mcp.py`; `tests/integration/test_mcp_router.py` round-trip updated to the structured contract. | [x] | Dev |
| T-MCP13.6 | Behavioral | • **Achieve:** a `\|` in `source_url` cannot split the citation-table row (PR #171 gemini review).<br>• **Deliver:** `::test_tools_call_retrieve_sanitizes_markdown_in_text` extended with a pipe-bearing URL (encoded `%7C` per T-MCP13.7's URL encoding); raw URL still reaches the frontend via `structuredContent`. | [x] | Dev |
| T-MCP13.7 | Behavioral | • **Achieve:** only `http(s)` `source_url` values are linkified — `javascript:` URLs render as plain title; `(` `)` space `\|` percent-encoded so the link destination cannot end early (PR #171 codex review).<br>• **Deliver:** `::test_tools_call_retrieve_does_not_linkify_non_http_urls`, `::test_tools_call_retrieve_encodes_markdown_breaking_url_chars`; `_safe_link_url()` in `routers/mcp.py`. | [x] | Dev |
| T-MCP13.8 | Behavioral | • **Achieve:** literal `<context>`/`</context>` tags inside titles/excerpts are neutralised to `&lt;…&gt;` so corpus text cannot prematurely close the wrapper (PR #171 codex review).<br>• **Deliver:** `::test_tools_call_retrieve_neutralizes_context_tags_in_text`; `_neutralize_context_tags()` in `routers/mcp.py`; raw values survive in `structuredContent`. | [x] | Dev |
| T-MCP13.9 | Behavioral | • **Achieve:** `initialize` negotiates the protocol revision — echoes a supported requested version (`2025-06-18`/`2025-03-26`/`2024-11-05`), falls back to latest for unsupported ones, instead of force-upgrading older clients (PR #171 codex review).<br>• **Deliver:** `tests/unit/test_mcp_initialize.py::test_initialize_echoes_supported_older_protocol_version` + `::test_initialize_unsupported_protocol_version_falls_back_to_latest`; `_SUPPORTED_PROTOCOL_VERSIONS` in `routers/mcp.py`. | [x] | Dev |



---

## Track T-CA — ChatAgent Proxy Endpoints

> Three proxy endpoints under `/chatagent/v1` that forward to external services.
> All share `CHATAGENT_AUTH` outbound header and `CHATAGENT_AP_NAME` config.
> Each route is conditionally registered based on its URL env var.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CA.S1 | Structural | • **Achieve:** `CHATAGENT_UPSTREAM_ERROR`, `CHATAGENT_TIMEOUT`, `CHATAGENT_RATE_LIMITED` in `HttpErrorCode`.<br>• **Deliver:** `src/ragent/errors/codes.py`. | [x] | Dev |
| T-CA.S2 | Structural | • **Achieve:** `ChatAgentRequest(ChatRequest)` with optional `session: str \| None`.<br>• **Deliver:** `src/ragent/schemas/chatagent.py`; `tests/unit/test_chatagent_schema.py`. | [x] | Dev |
| T-CA.R1 | Behavioral | • **Achieve:** `POST /chatagent/v1` proxies to `CHATAGENT_API_URL`; user_id (RAGENT_JWT_CLAIM_USER_ID), session generation, rate limiting, error mapping.<br>• **Deliver:** `src/ragent/routers/chatagent.py::create_chatagent_router`; `tests/unit/test_chatagent_router.py` POST tests. | [x] | Dev |
| T-CA.R2 | Behavioral | • **Achieve:** `GET /chatagent/v1/sessionList` proxies to `CHATAGENT_SESSIONLIST_API_URL`; injects user/apName.<br>• **Deliver:** route in `chatagent.py`; GET sessionList unit tests. | [x] | Dev |
| T-CA.R3 | Behavioral | • **Achieve:** `GET /chatagent/v1/session` proxies to `CHATAGENT_SESSION_API_URL`; injects user/apName/session.<br>• **Deliver:** route in `chatagent.py`; GET session unit tests. | [x] | Dev |
| T-CA.I1 | Behavioral | • **Achieve:** Routes registered conditionally by URL env var; integration tests via TestClient + mocked httpx.<br>• **Deliver:** `tests/integration/test_chatagent_endpoint.py`. | [x] | Dev |
| T-CA.W1 | Behavioral | • **Achieve:** Composition root reads 5 new env vars; app.py registers router when any URL is set.<br>• **Deliver:** `composition.py` Container fields + build_container(); `app.py` registration block. | [x] | Dev |
| T-CA.D1 | Structural | • **Achieve:** All new env vars documented (B28); API.md + third-party API doc updated.<br>• **Deliver:** `docs/spec/env_vars.md`, `docs/API.md`, `docs/00_rule_third_party_api.md`. | [x] | Dev |
| T-CA.R4 | Behavioral | • **Achieve:** `POST /chatagent/v1` response body includes `session` field — the value used for this request (either caller-supplied or auto-generated via `new_id()`).<br>• **Deliver:** `"session"` key added to `JSONResponse` dict in `chatagent.py`; two new tests in `test_chatagent_router.py` covering supplied vs generated session echo. | [x] | Dev |
| T-CA.R5 | Behavioral | • **Achieve:** `PUT /chatagent/v1/session` proxies to `CHATAGENT_SESSION_API_URL`; injects `apName`/`user`; forwards `session`+`sessionName` from request body; `SessionRenameRequest` schema (`session`, `sessionName` required).<br>• **Deliver:** route + schema in `chatagent.py`/`schemas/chatagent.py`; unit tests in `test_chatagent_router.py`; integration tests in `test_chatagent_endpoint.py`. | [x] | Dev |
| T-CA.R6 | Behavioral | • **Achieve:** `DELETE /chatagent/v1/session` proxies to `CHATAGENT_SESSION_API_URL`; injects `apName`/`user`; forwards `session` from request body; `SessionDeleteRequest` schema (`session` required).<br>• **Deliver:** route + schema in `chatagent.py`/`schemas/chatagent.py`; unit tests in `test_chatagent_router.py`; integration tests in `test_chatagent_endpoint.py`. | [x] | Dev |
| T-CA.R7 | Behavioral | • **Achieve:** `_proxy_write` handles empty/204 upstream responses — forwards `Response(status_code)` instead of calling `.json()` on empty body, preventing false 502.<br>• **Deliver:** guard in `_proxy_write`; `test_session_rename_no_content_204_returns_204` and `test_session_delete_no_content_204_returns_204`. | [x] | Dev |


---

## Track T-DEL1 — VectorExtractor.delete() candidate-index alignment (issue #147)

> `VectorExtractor.delete()` only cleans the stable index. During CANDIDATE/CUTOVER lifecycle,
> `DocumentEmbedder._run_dual` writes to both `stable_index` and `candidate_index`.
> Deleting a document in that window orphans chunks in the candidate index.
> Fix: inject `ActiveModelRegistry` (via `_IndexProvider` Protocol) into `VectorExtractor` so
> `delete()` fans out across all live write targets. Decision Log: B62.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-DEL1.1 | Behavioral | • **Achieve:** `VectorExtractor.delete()` issues `delete_by_query` for every live index (`stable_index` + `candidate_index` when not None).<br>• **Deliver:** `_IndexProvider` Protocol in `vector.py`; `registry: _IndexProvider \| None` constructor arg; `_delete_indices()` helper; tests in `tests/unit/test_vector_extractor.py` — stable-only and dual-index cases. | [x] | Dev |
| T-DEL1.2 | Behavioral | • **Achieve:** Composition wires `ActiveModelRegistry` into `VectorExtractor` so production deployments use the live-index fan-out path.<br>• **Deliver:** Reorder `composition.py` to build `embedding_registry` before `VectorExtractor`; pass `registry=embedding_registry`. Update `tests/unit/test_chunks_index_env_audit.py` to assert `registry` kwarg is wired. | [x] | Dev |

## Track T-DEL2 — PR #149 review findings (Gemini + Codex)

> Two review findings on PR #149:
> (1) Gemini: `_delete_indices()` must deduplicate when `candidate == stable`.
> (2) Codex: `_PerTickRunner._tick()` must refresh `embedding_registry` before `fan_out_delete`
>     so the cold-cache path during CANDIDATE/CUTOVER doesn't silently skip candidate cleanup.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-DEL2.1 | Behavioral | • **Achieve:** `_delete_indices()` deduplicates — if `candidate_index == stable_index`, only one `delete_by_query` call is issued.<br>• **Deliver:** Guard `candidate and candidate != stable` in `_delete_indices()`; `test_delete_indices_deduplicates_when_candidate_equals_stable` in `test_vector_extractor.py`. | [x] | Dev |
| T-DEL2.2 | Behavioral | • **Achieve:** Reconciler warms `ActiveModelRegistry` before fan-out so `VectorExtractor.delete()` sees live indices during CANDIDATE/CUTOVER.<br>• **Deliver:** `await container.embedding_registry.refresh()` in `_PerTickRunner._tick()`; source-inspection test `test_per_tick_runner_refreshes_embedding_registry` in `test_retired_embedding_sweep.py`. Update `test_engine_pool_config.py` mock. | [x] | Dev |

---

## Track T-CAv2 — ChatAgent v2 Raw-Proxy Endpoint

> Source: 2026-06-03 feature request.
> Adds `POST /chatagent/v2` — a thin raw-proxy that accepts the upstream payload shape
> directly (minus the three server-managed fields `apName`/`user`/`userToken`), forwards
> it verbatim, and streams the upstream response back to the client unchanged.
>
> **Key differences from v1:**
> - Input schema mirrors the upstream wire format; server injects `apName`, `user`,
>   `userToken` before forwarding (both streaming and non-streaming paths).
> - Output is the upstream response forwarded as-is (no JSON reshaping).
> - `stream: true` triggers chunked forwarding via `StreamingResponse`.

### Input / Output Samples

#### Non-streaming (`stream: false`)

**Client → ragent**
```
POST /chatagent/v2
Content-Type: application/json
X-User-Id: alice

{"metadata": {"session": "sess-abc"}, "inputData": {"message": "What are the product features?"}, "stream": false}
```

**ragent → upstream** *(server injects apName, user, userToken — in both streaming and non-streaming)*
```json
{"metadata": {"apName": "MyApp", "session": "sess-abc", "user": "alice", "userToken": "Bearer eyJ..."}, "inputData": {"message": "What are the product features?"}, "stream": false}
```

**Upstream → client** *(forwarded byte-for-byte)*
```json
{"returnCode": 96200, "returnData": {"messages": [{"role": "assistant", "content": "The features include...", "message_id": "m1"}]}}
```

#### Streaming (`stream: true`)

**Client → ragent**
```
POST /chatagent/v2
Content-Type: application/json
X-User-Id: alice

{"metadata": {"session": "sess-abc"}, "inputData": {"message": "Summarise the release notes."}, "stream": true}
```

**Upstream → client** *(Transfer-Encoding: chunked; Content-Type copied from upstream)*
```
{"returnCode": 96200, "returnData": {"delta": "The release notes "}}
{"returnCode": 96200, "returnData": {"delta": "cover..."}}
{"returnCode": 96200, "returnData": {"done": true}}
```

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv2.S1 | Structural | • **Achieve:** Accept arbitrary JSON body (`dict[str, Any]`) — no schema enforcement; server injects `apName`/`user`/`userToken` into `metadata`, extracts `session` and `stream` by key lookup, forwards the rest verbatim.<br>• **Deliver:** `src/ragent/routers/chatagent_v2.py` uses raw dict body; `ChatAgentV2*` schema models removed. | [x] | Dev |
| T-CAv2.R1 | Behavioral | • **Achieve:** `POST /chatagent/v2` non-streaming — inject server fields, POST to upstream, forward raw bytes + upstream `Content-Type`.<br>• **Deliver:** `src/ragent/routers/chatagent_v2.py::create_chatagent_v2_router()`; unit tests for happy path, timeout, upstream error. | [x] | Dev |
| T-CAv2.R2 | Behavioral | • **Achieve:** `POST /chatagent/v2` streaming — `stream: true` uses unified `build_request`+`send(stream=True)` path, validates upstream status before committing HTTP response, returns `StreamingResponse` via `iterate_in_threadpool` forwarding each byte-chunk immediately with upstream Content-Type.<br>• **Deliver:** Streaming branch in `chatagent_v2.py`; unit tests assert all chunks forwarded, upstream Content-Type forwarded, upstream error returns 502 before first byte. | [x] | Dev |
| T-CAv2.R3 | Behavioral | • **Achieve:** Rate limiting with key `"chatagent:{user_id}"` → 429 `CHATAGENT_RATE_LIMITED`.<br>• **Deliver:** Unit test `test_rate_limited_returns_429`. | [x] | Dev |
| T-CAv2.W1 | Behavioral | • **Achieve:** Router registered in `bootstrap/app.py` under the existing `CHATAGENT_API_URL` guard.<br>• **Deliver:** `app.py` wiring; `tests/integration/test_chatagent_v2_endpoint.py` — non-streaming, streaming, error paths, session auto-generation. | [x] | Dev |

---

## Track T-OPS: Batch Retry Operation API

> Adds `POST /ops/v1/retry` so operators can immediately force-retry documents in UPLOADED/PENDING/FAILED states with a `dry_run` preview mode.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-OPS.R1 | Behavioral | • **Achieve:** `DocumentRepository.count_by_statuses()` + `list_by_statuses()` with optional source_app/source_id/created_after filters.<br>• **Deliver:** `src/ragent/repositories/document_repository.py` — two new methods + `_status_filter_clauses` helper; `tests/unit/test_document_repository_ops.py` (12 tests). | [x] | Dev |
| T-OPS.R2 | Behavioral | • **Achieve:** `IngestService.batch_rerun()` — dry_run preview, per-doc mark+enqueue loop, before/after count snapshot.<br>• **Deliver:** `src/ragent/services/ingest_service.py`; `tests/unit/test_ingest_service_batch_rerun.py` (11 tests). | [x] | Dev |
| T-OPS.R3 | Behavioral | • **Achieve:** `POST /ops/v1/retry` endpoint with OpsRetryRequest/OpsRetryResponse schemas.<br>• **Deliver:** `src/ragent/routers/admin_ops.py`; `tests/unit/test_admin_ops_router.py` (10 tests). | [x] | Dev |
| T-OPS.W1 | Behavioral | • **Achieve:** Register admin_ops router in `bootstrap/app.py`.<br>• **Deliver:** `app.include_router(create_admin_ops_router(svc=ingest_svc))` wired after ingest router. | [x] | Dev |
| T-OPS.R4 | Behavioral | • **Achieve:** PR review hardening — entry log before DB call, per-item dispatch log, operator_id audit field, extra-field rejection (`ConfigDict(extra="forbid")`), counts keyed by requested statuses, `idx_status_created` index for `(status, created_at)` queries.<br>• **Deliver:** `ingest_service.py`, `admin_ops.py`, `migrations/012_documents_status_created_index.sql`, `schema.sql`; 16 tests in `test_ingest_service_batch_rerun.py`, 14 tests in `test_admin_ops_router.py`. | [x] | Dev |

---

## Track T-MCP-REG — MCP v1 Tool Registration Best Practices

> Source: 2026-06-07 refactor. Eliminates string-literal JSON schema drift in `mcp/v1` by adopting `mcp.types.Tool` + Pydantic-derived `inputSchema` as the single source of truth.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-MCP-REG.1 | Behavioral | • **Achieve:** Replace hand-written `_RETRIEVE_TOOL_SCHEMA` dict with `mcp.types.Tool` descriptor; derive `inputSchema` from `_RetrieveArgs(RetrieveRequest)` via `_build_mcp_input_schema()`; derive runtime validator from `RETRIEVE_TOOL.inputSchema`; introduce `_ALL_TOOLS` registry so adding a new tool is a one-line change.<br>• **Deliver:** `src/ragent/routers/mcp_tools/__init__.py` + `src/ragent/routers/mcp_tools/retrieve.py` (new); `src/ragent/routers/mcp.py` (uses registry); `tests/unit/test_mcp_tools_list.py` (deep-equals against model_dump + description coverage test). | [x] | Dev |
| T-MCP-REG.2 | Behavioral | • **Achieve:** Add agent-oriented `description=` to all six fields of `RetrieveRequest` so both the OpenAPI docs and the MCP `inputSchema` carry actionable guidance for AI callers.<br>• **Deliver:** `src/ragent/schemas/retrieve.py` — all six `Field(...)` calls gain `description=`; `docs/spec/mcp_server.md §3.8.3` updated to reflect new descriptions. | [x] | Dev |
| T-MCP-REG.3 | Behavioral | • **Achieve:** Fix `_build_mcp_input_schema`: strip `"default": null` after collapsing `anyOf:[{type:T},{type:null}]` — advertised null defaults were contradictory with `type:T` and caused Draft7Validator to reject explicit `null` submissions with -32602.<br>• **Deliver:** `src/ragent/routers/mcp_tools/retrieve.py` (4-line fix); `tests/unit/test_mcp_tools_list.py::test_retrieve_optional_fields_have_no_null_default`; `docs/spec/mcp_server.md §3.8.3` (remove `"default": null` from optional fields). | [x] | Dev |
| T-MCP-REG.4 | Behavioral | • **Achieve:** Improve `retrieve` tool description UX for AI agents: (1) replace impl-detail "hybrid vector + BM25 search" with behavior-oriented "hybrid semantic + keyword search"; (2) remove misleading `source_app` examples ('confluence', 'jira') and guide agent to derive valid values from previous result metadata.<br>• **Deliver:** `src/ragent/routers/mcp_tools/retrieve.py` (tool description); `src/ragent/schemas/retrieve.py` (source_app description); `docs/spec/mcp_server.md §3.8.3` (synced). | [x] | Dev |

---

## Track T-CAv3 — ChatAgent v3 (twp-ai protocol proxy)

> Source: 2026-06-08 design session. Adds `POST /chatagent/v3` — same upstream proxy
> mechanism as v2 (shares `CHATAGENT_API_URL` / `CHATAGENT_AUTH` / rate limit), but the
> **wire contract is the twp-ai protocol**: request is a twp-ai `RunAgentInput`, response is
> a twp-ai SSE event stream. ragent owns the bidirectional conversion.
>
> **Abstraction:** mirrors `DirectLLMAgent` + caller split.
> - `ADKAgent` (new, `packages/twp-ai/src/twp_ai/agents/adk.py`) — owns the twp-ai event flow
>   (`RUN_STARTED` → `TEXT_MESSAGE_START`/`CONTENT`/`END` → `RUN_FINISHED`; any caller exception
>   → `RUN_ERROR`). Pure proxy: no system prompt, no tool loop.
> - `ADKCaller` protocol (new, `packages/twp-ai/src/twp_ai/callers/adk.py`, pure abstract) —
>   `stream_deltas(request, model) -> Generator[str]`. Concrete httpx impl lives ragent-side.
>
> **Conversion rules (locked):**
> - Request: `RunAgentInput` → a `<hidden>`-wrapped `context`/`state` preamble (no persona, no
>   tools — the upstream owns its persona/memory) is prepended to the last `role="user"` message
>   content → upstream `inputData.message` (single-field wire); no context/state ⇒ bare user text.
>   The `<hidden>` block (short-term fix) lets the frontend strip context/state from rendered
>   history; wrapper tokens inside the payload are neutralized so it can't close early.
>   metadata injection mirrors v2 (`apName`/`user`/`userToken`/`session`), with `session = threadId`.
>   `model` not forwarded (upstream decides, same as v2). Always `stream: true`.
> - Response: upstream `{"returnCode":96200,"returnData":{"delta":...}}` → `TEXT_MESSAGE_CONTENT`;
>   `{"returnData":{"done":true}}` → `TEXT_MESSAGE_END` + `RUN_FINISHED`. `messageId` minted by ADKAgent.
> - Errors: **all** failures (rate-limit, timeout, upstream non-96200/5xx) surface as a `RUN_ERROR`
>   SSE event over a 200 stream — never an HTTP 4xx/5xx code.
> - context/state: folded into a `<hidden>`-wrapped preamble prepended to the user message (T-CAv3.6).
>   tools/forwardedProps: accepted but not forwarded; tool-call continuation deferred.

| # | Category | Task | Status | Owner |
|---|---|---|:---:|---|
| T-CAv3.1 | Red+Green | • **Achieve:** `ADKCaller` protocol — structural `stream_deltas(request, model) -> Generator[str]`.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/callers/adk.py`; covered via ADKAgent tests using a fake caller. | [x] | Dev |
| T-CAv3.2 | Red+Green | • **Achieve:** `ADKAgent.run()` emits the twp-ai text lifecycle from caller deltas; caller exception → `RUN_ERROR` carrying `error_code` when present.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/agents/adk.py`; `packages/twp-ai/tests/test_adk_agent.py` — happy path event order, per-delta CONTENT, error→RUN_ERROR. | [x] | Dev |
| T-CAv3.3 | Red+Green | • **Achieve:** ragent-side concrete `ADKCaller` — builds upstream payload (last-user-message → `inputData.message`, metadata `session=threadId`, `stream:true`), parses `returnData.delta`/`done`, raises `UpstreamTimeoutError`/`UpstreamServiceError`.<br>• **Deliver:** `src/ragent/clients/adk_caller.py`; `tests/unit/test_adk_caller.py` — payload shape, delta parsing, done stop, timeout→504-coded exc, non-96200→502-coded exc. | [x] | Dev |
| T-CAv3.4 | Red+Green | • **Achieve:** `POST /chatagent/v3` — `get_user_id` dep, builds `RunAgentInput`, per-request `ADKCaller`, streams `ADKAgent` events as `text/event-stream`; rate-limit exceeded → 200 SSE with single `RUN_ERROR` (`CHATAGENT_RATE_LIMITED`).<br>• **Deliver:** `src/ragent/routers/chatagent_v3.py::create_chatagent_v3_router`; `tests/unit/test_chatagent_v3_router.py` — happy path event stream, server-field injection, rate-limit→RUN_ERROR, upstream error→RUN_ERROR. | [x] | Dev |
| T-CAv3.W1 | Behavioral | • **Achieve:** Register `/chatagent/v3` in `bootstrap/app.py` under the existing `CHATAGENT_API_URL` guard (shares http_client/auth/rate_limiter/ap_name).<br>• **Deliver:** `app.py` wiring; `tests/integration/test_chatagent_v3_endpoint.py` — full TestClient flow with mocked httpx streaming, RUN_ERROR on upstream failure. | [x] | Dev |
| T-CAv3.D1 | Structural | • **Achieve:** Document the v3 contract.<br>• **Deliver:** `docs/00_spec.md` (v3 System Interface + Scenario rows), `docs/API.md` (endpoint + samples). No new env var (shares v2 config). | [x] | Dev |
| T-CAv3.5 | Red+Green | • **Achieve:** Map the upstream `planner` node (the agent's plan/reasoning step) to a reasoning block instead of a `TEXT_MESSAGE` block: `REASONING_START` → `REASONING_MESSAGE_START`/`REASONING_MESSAGE_CONTENT`*/`REASONING_MESSAGE_END` → `REASONING_END`. Streams per-delta; closes the reasoning block at the messageId boundary before any following `TEXT_MESSAGE`. Other nodes unchanged.<br>• **Deliver:** `packages/twp-ai/src/twp_ai/events.py` (5 new events + union); `packages/twp-ai/src/twp_ai/agents/adk.py` (`_relay` block-kind tracking); `packages/twp-ai/tests/test_adk_agent.py` + `tests/unit/test_chatagent_v3_router.py` (reasoning lifecycle, streamed deltas, planner→summarizer transition); `docs/00_spec.md` §3.4.7 + `docs/API.md` Example 2 + event-type list. | [x] | Dev |
| T-CAv3.6 | Red+Green | • **Achieve:** Surface the client-supplied `context`/`state` (previously dropped) to the upstream by prepending a labelled `Context:`/`State:` preamble to the last user message → `inputData.message`. No persona and no tool enumeration — the upstream is a general, tool-capable agent that owns its persona and keeps memory by `session`; no context/state ⇒ bare user text (plain pass-through).<br>• **Deliver:** `src/ragent/clients/adk_caller.py` (`_compose_message` + `_context_preamble`); `tests/unit/test_adk_caller.py` + `tests/unit/test_chatagent_v3_router.py` (preamble fold, tools excluded, pass-through when empty); `docs/00_spec.md` §3.4.7 + `docs/API.md`. | [x] | Dev |
