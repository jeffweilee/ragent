# Discussion: Phase 1 — Pipeline + Plugin Skeleton Kickoff

> Source: `docs/draft.md` (分散式 RAG Agent 系統計畫書)
> Date: 2026-05-03
> Mode: Agent Team (6 voting members + Master)

---

## Master's Opening

**Triggered rules:**
- `docs/rule.md` §Workflow: Initiation → Debate → Conflict → Vote → Output
- `CLAUDE.md` §TDD Workflow + §Tidy First (structural vs behavioral)
- `draft.md` §Phase 1.0 出口準則: ingest 成功率 ≥ 99%、金標題庫 50 題 top-3 ≥ 70%

**Topic:** 將 `draft.md` 展開為可執行的 `00_spec.md` (WHAT) 與 `00_plan.md` (TDD checklist)，並就 Phase 1 範圍、邊界與第一刀切入點達成共識。

**Core principle (draft.md L170):** Pipeline + Plugin — 兩條 Pipeline 的骨架在 P1 一次成形，後續演進 = 掛 plugin，主架構零改動。

---

## Round 1 — Role Perspectives

- 🏗 **Architect**: [Pro] 必須先凍結 `ExtractorPlugin` Protocol v1 (name / required / queue / extract / delete / health)，否則 Vector + Stub Graph 會分歧。[Con] 過早抽象風險：若 Graph 真實需求未明，介面可能過設計。**主張**：以 Vector Extractor 真實需求為唯一輸入定義介面，Stub 只實作介面不擴展之。
- ✅ **QA**: [Pro] Given-When-Then 必須先寫；Phase 1 出口的「ingest 成功率 ≥ 99%」必須有可量測測試 (Reconciler 補發 + idempotency)。[Con] 50 題金標題庫在 P1 第一週無法備齊，建議降為「pipeline 端到端 happy path + 1 條失敗復原路徑」作為 Definition of Done 的 P1.0 子集，金標題庫評測列為 P1 出口閘 (P1 收尾才驗)。
- 🛡 **SRE**: [Pro] Redis 雙實例 (broker + limiter) 與 Reconciler 5 分鐘冪等補發必須在 P1 day-1 即就位，否則任務會靜悄悄消失。[Con] OTEL 全鏈路會拖慢 P1，建議僅啟 Haystack 原生 auto-trace + FastAPI 中介層，不寫自訂 span。
- 🔍 **Reviewer**: [Pro] Tidy First — 強制 STRUCTURAL/BEHAVIORAL commit 分離，Plugin Protocol 屬 STRUCTURAL，Vector Extractor 實作屬 BEHAVIORAL。[Con] 反對在 P1 引入任何「為 P3 準備」的程式碼 (LightRAG 介面、圖譜欄位)，違反 YAGNI。
- 📋 **PM**: [Pro] P1 工期 5–7 週需切成 6 個週迭代里程碑：(W1) Spec/Plan + Skeleton；(W2) Plugin Protocol + Vector Extractor TDD；(W3) Ingest Pipeline E2E；(W4) Chat Pipeline (Vector+BM25 並行 + Joiner)；(W5) JWT/ACL 雙層過濾；(W6) Reconciler + 出口驗收。[Con] MCP Tool「雛形」字眼模糊，建議 P1 只做 schema 定義，實作延到 P2。
- 💻 **Dev (Fullstack Senior)**: [Pro] Python 3.12 + uv + ruff + pytest 工具鏈先行，commit 前強制 `uv run ruff format/check + pytest` (rule.md §Command)。[Con] Haystack 2.x AsyncPipeline 在 P1 是否啟用 async 模式需單獨 spike — async 會增加 ingest worker 整合成本，建議 P1 先 sync，P2 切 async。

---

## Conflict Identification

| # | Issue | 正方 | 反方 |
|---|---|---|---|
| C1 | 50 題金標題庫是否 P1 day-1 必備 | Architect, PM | QA, Dev |
| C2 | MCP Tool P1 範圍 (雛形 vs schema-only) | Architect | PM, Reviewer, Dev |
| C3 | AsyncPipeline P1 啟用 vs 延後 | Architect | Dev, SRE, QA |
| C4 | Stub Graph Extractor 是否寫程式碼 | Architect (留空 stub) | Reviewer (YAGNI 反對) |

---

## Voting Results

| Issue | 🏗 | ✅ | 🛡 | 🔍 | 📋 | 💻 | Result |
|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| C1 降級為「P1 收尾驗收」非 day-1 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 5/6** |
| C2 P1 只做 MCP schema，實作 P2 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 5/6** |
| C3 P1 sync pipeline，P2 切 async | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 5/6** |
| C4 Stub Graph 僅實作 Protocol 介面，回傳空 list | ✅ | ✅ | ✅ | ✅ (符合 plugin 骨架日 1 即運作 draft.md L176) | ✅ | ✅ | **Pass 6/6** |

---

## Decision Summary

**✅ Approved (Phase 1 Scope Frozen):**

1. **Pipeline 骨架 (W1–W4)**：Ingest (Converter→Cleaner→LanguageRouter→CN/EN Splitter→Embedder→ES) + Chat (QueryEmbedder→{ESVector∥ESBM25}→DocumentJoiner RRF) **以 sync Haystack Pipeline 先行**，async 切換留 P2。
2. **Plugin Protocol v1 凍結 (W2)**：`ExtractorPlugin` 介面 = `name: str`, `required: bool`, `queue: str`, `extract(doc_id) -> None`, `delete(doc_id) -> None`, `health() -> bool`。
3. **首批 Plugin (W2–W3)**：
   - `VectorExtractor` (required=True)：Batch embed → ES bulk index。
   - `StubGraphExtractor` (required=False)：實作介面、`extract` 直接 `return None`、`health()` 回 True；確保 Chat 端 fallback 路徑 day-1 可運作。
4. **韌性 (W6)**：Redis 雙實例 (broker + rate-limiter)；Reconciler 每 5 分鐘掃 PENDING > 5 min → re-kiq (冪等 by `doc_id + attempt`)；attempt > 5 → FAILED + log alert。
5. **權限 (W5)**：JWT 驗證 + ACL 雙層 (查詢前 ES filter on `acl_user_ids`，回傳前 post-filter 再驗 doc_id ∈ 白名單)。
6. **API (W4–W5)**：`POST /ingest` (multipart, JWT) → 202 + task_id；`POST /chat` (SSE, JWT) → delta + done(answer, sources)；`POST /mcp/tools/rag` **僅定義 OpenAPI schema**，handler 回 501 Not Implemented (P1)。
7. **觀測**：僅啟 Haystack auto-trace + FastAPI OTEL middleware，不寫自訂 span。
8. **品質閘**：commit 前強制 `uv run ruff format . && uv run ruff check . --fix && uv run pytest`。
9. **TDD 紀律**：每個 plan.md `[ ]` 都對應一個 Red→Green→Refactor 循環；STRUCTURAL/BEHAVIORAL commit 嚴格分離 (`[STRUCTURAL]` / `[BEHAVIORAL]` prefix)。

**❌ Out of Phase 1 (Deferred):**

- Rerank API、ConditionalRouter 意圖分流、RAGAS 評測 → **P2**
- LightRAGRetriever、真實 GraphExtractor、Graph DB 選型 → **P3** (with gate)
- AsyncPipeline 切換 → **P2**
- MCP Tool 真實 handler → **P2**

**🎯 Phase 1 Exit Criteria (P1 收尾驗收):**

| 指標 | 門檻 | 度量方式 |
|---|---|---|
| ingest 成功率 | ≥ 99% | E2E 100 docs 注入測試，count(status=READY) / 100 |
| 金標題庫 50 題 top-3 命中 | ≥ 70% | `tests/e2e/test_golden_set.py` (P1 收尾週備齊) |
| Reconciler 復原 | 100% | Chaos test：人工殺 worker，Reconciler 必須在 ≤ 10 min 內補發 |
| 權限隔離 | 0 越權 | Integration test：user A 不可檢索 user B 私有 doc |
| Lint / Format / Test | 全綠 | CI pipeline |

**Trade-offs Accepted:**

- 暫不啟用 AsyncPipeline → ingest 吞吐受限，但降低 P1 整合風險。
- Stub Graph 占位程式碼會在 P3 改寫 → 接受少量「日後重寫」成本，換取 Chat fallback 路徑 day-1 即運作。
- 金標題庫延到 P1 收尾 → 早期週次無法回歸品質，以「pipeline E2E happy + 1 失敗路徑」替代。

---

## Pending Items

無未決事項。所有衝突點均於 Round 1 收斂。**進入 plan.md 執行階段。**
