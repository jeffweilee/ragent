# 分散式 RAG Agent 系統計畫書

## 系統概覽

基於 Python 3.12 的企業內部知識檢索後端,整合向量搜尋、全文檢索與意圖路由,Phase 3 擴充圖譜推理。核心方針:**簡單、不出錯、有韌性、能復原**。所有 AI 推論(Embedding / Rerank / LLM)均呼叫第三方 API,系統不部署本地模型。

---

## 架構圖

### 路徑 A — Ingest Flow(文件入庫,非同步)

```
Client
  │
  │ POST /ingest  (multipart, JWT)
  ▼
┌───────────────────────────────────────────────┐
│  FastAPI  /ingest endpoint                    │
│  - JWT 驗證 + ACL 寫入 (誰可讀此文件)          │
│  - 存原檔到 MinIO                             │
│  - 寫 documents 表 (status=UPLOADED, ACL)     │
│  - kiq ingest_pipeline(doc_id)                │
│  - 立即回 { task_id }                         │
└───────────────────┬───────────────────────────┘
                    │ TaskIQ
                    ▼
┌───────────────────────────────────────────────┐
│  REDIS BROKER (AOF + noeviction)              │
│  queue: ingest.pipeline                       │
└───────────────────┬───────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│  MAIN WORKER  —  Haystack Ingest Pipeline     │
│  Converter → Cleaner → LanguageRouter         │
│    ├─ CN → HanLP ChineseDocumentSplitter      │
│    └─ EN → NLTK DocumentSplitter              │
│  → CustomEmbedder*  (no IO write)             │
│  → 寫 chunks 表 (MariaDB)                      │
│  → fan-out kick                               │
└───┬──────────────────────────────────────┬────┘
    │                                      │
    ▼                                      ▼
┌──────────────────────┐        ┌──────────────────────┐
│  VECTOR WORKER       │        │  GRAPH WORKER  [P3]  │
│  ExtractorPlugin     │        │  ExtractorPlugin     │
│  (Required)          │        │  (Optional)          │
│  Batch Embed* → ES   │        │  Stub [P1]           │
│  bulk index          │        │  LightRAG [P3]       │
│  → status=READY      │        │  → Graph DB          │
└──────────────────────┘        └──────────────────────┘

┌───────────────────────────────────────────────┐
│  RECONCILER (5 min)                           │
│  scan PENDING > 5 min → re-kiq (idempotent)   │
│  attempt > 5 → FAILED + Alert                 │
└───────────────────────────────────────────────┘
```

### 路徑 B — Chat Flow(問答串流,同步 SSE)

```
Client
  │
  │ POST /chat  (JWT, query)         POST /mcp/tools/rag  (same pipeline)
  ▼                                  ▼
┌───────────────────────────────────────────────┐
│  FastAPI  /chat (SSE)  &  MCP Tool endpoint   │
└───────────────────┬───────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│  AUTH & PERMISSION FILTER                     │
│  - JWT 驗證                                    │
│  - 取 user 可存取的 doc_id 白名單              │
│  - 注入 ES filter (查詢前過濾)                 │
└───────────────────┬───────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│  INTENT CLASSIFICATION  [P2]                  │
│  ConditionalRouter                            │
│    ├─ 翻譯/摘要/格式 → Direct LLM* (skip)     │
│    └─ 知識問答      → Retrieval Pipeline      │
└───────────────────┬───────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│  RETRIEVAL PIPELINE  (Haystack AsyncPipeline) │
│  QueryEmbedder*                               │
│    │                                          │
│    ├──→ ESVectorRetriever  ∥                  │
│    ├──→ ESBM25Retriever    ∥                  │
│    └──→ LightRAGRetriever [P3, 200ms TO→[]]   │
│    ▼                                          │
│  DocumentJoiner (RRF)                         │
│    ▼                                          │
│  Rerank API* [P2]  (top-50 → 8)               │
│    ▼                                          │
│  POST-FILTER: 結果再次過濾 doc_id ∈ 白名單     │
│    ▼                                          │
│  LLM API*  stream                             │
└───────────────────┬───────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│  SSE Response (chat) / JSON (MCP)             │
│  delta: { text }                              │
│  done : { answer, sources:[{id,title,url}] }  │
└───────────────────────────────────────────────┘
```

### 共用基礎設施

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Storage Layer                                                              │
│  ┌────────────┐  ┌──────────────────────┐  ┌────────┐  ┌─────────────────┐ │
│  │  MariaDB   │  │  Elasticsearch 9.x   │  │ MinIO  │  │ Graph DB [P3]   │ │
│  │ - docs+ACL │  │  vector + BM25       │  │ files  │  │ Neo4j/ArcadeDB  │ │
│  │ - chunks   │  │  graph_indexed:bool  │  │        │  │ entity/relation │ │
│  │ - chat     │  │  doc_id metadata     │  │        │  │                 │ │
│  │ - eval     │  └──────────────────────┘  └────────┘  └─────────────────┘ │
│  └────────────┘                                                             │
│                                                                             │
│  Third-party API Gateway:  Rate Limit + Circuit Breaker + Retry            │
│  * Embedding API  │  * Rerank API  │  * LLM API                            │
│                                                                             │
│  Observability:  OTEL Auto-Trace → Grafana Tempo + Prometheus              │
└─────────────────────────────────────────────────────────────────────────────┘
[P1] 5-7w  │  [P2] +3w  │  [P3] +4-6w conditional  │  * = third-party API
```

---

## API 回應格式

```json
// SSE 串流事件 (/chat)
{ "event": "delta",  "data": { "text": "根據文件..." } }
{ "event": "delta",  "data": { "text": "顯示結果如下" } }
{ "event": "done",   "data": {
    "answer": "...",
    "sources": [
      { "id": "doc_abc123", "title": "2024 產品規格書", "url": "/docs/doc_abc123" },
      { "id": "doc_xyz456", "title": "API 整合手冊",   "url": "/docs/doc_xyz456" }
    ]
  }
}

// MCP Tool 回應 (同結構,非串流)
{ "answer": "...", "sources": [ { "id", "title", "url" } ] }
```

---

## 技術棧

- **框架**:Python 3.12 / FastAPI / Haystack 2.x(AsyncPipeline + SuperComponent)
- **任務**:TaskIQ + Redis Broker + Redis Rate Limiter(分兩實例)
- **儲存**:Elasticsearch 9.x / MariaDB 10.6 / MinIO
- **對外介面**:REST API / SSE / MCP Tool(無前端)
- **觀測**:OTEL + Grafana Tempo + Prometheus

---

## 交付階段

**核心精神:Pipeline + Plugin** — 兩條 Pipeline(Ingest / Chat)的骨架在 P1 一次成形,後續所有演進都是「掛載新 Plugin 或切換 SuperComponent」,主架構零改動。Phase 推進 = 啟用更多能力,不是重寫系統。

### Phase 1.0 — 核心 MLP(5–7 週)

- **Pipeline 骨架成形**:Ingest Pipeline 串好 Converter → Cleaner → LanguageRouter → CN/EN Splitter → Embedder;Chat Pipeline 串好 ES Vector + BM25 並行 + Joiner
- **Plugin Protocol v1 凍結**:`ExtractorPlugin` 介面釘死(name / required / queue / extract / delete / health),日後新增 plugin 不動介面
- **首批 Plugin 上線**:Vector Extractor(必要,寫 ES);Stub Graph Extractor(占位,讓 Chat 端 fallback 路徑 day-1 即可運作)
- **韌性骨架就位**:Redis broker / limiter 分兩實例;Reconciler 每 5 分鐘冪等補發卡住任務;OTEL 採 Haystack 原生自動 trace
- **權限模型上線**:JWT + ACL 雙層過濾(查詢前 ES filter + 回傳前再檢查)
- **API 介面定型**:`/ingest`、`/chat` SSE(含 sources)、MCP Tool 雛形
- **出口**:ingest 成功率 ≥ 99%、金標題庫 50 題 top-3 ≥ 70%

### Phase 2 — 生產品質(+3 週)

- **掛載新 Plugin**:Rerank API Component 接入 SuperComponent(`HybridRetrieverWithRerank`),import 換一行即升級
- **Chat Pipeline 增加意圖分類層**:`ConditionalRouter` 依 P1 真實 query 規則分流(翻譯 / 摘要 / 格式 → Direct LLM,跳過 retrieval)
- **MCP Tool endpoint 正式上線**,Claude Desktop 等可直連
- **品質閘道建立**:RAGAS 評測進 CI,大檔分頁 Streaming,Chaos Test 驗證降級
- **無本地模型**:Reranker 持續用第三方 API,系統不引入 GPU
- **出口**:首 token p95 < 5s、top-3 ≥ 85%、RAGAS faithfulness ≥ 0.80

### Phase 3 — Graph 增強(+4–6 週,條件啟動)

- **替換現有 Plugin**:Stub Graph Extractor 換成真實 GraphExtractor(實作不變介面);Chat Pipeline 切換為 `HybridRetrieverWithGraph` SuperComponent
- **圖資料庫選型 PoC**:候選 Neo4j Community / ArcadeDB / Memgraph,寫入 ADR
- **Graph 治理機制**:Entity 軟刪除 + ref_count + GC + 對賬排程
- **啟動 Gate**:P2 穩定運行 ≥ 4 週、純 hybrid 對關係題效果不足、stakeholder 共識值得投資、選型決策完成
- **可不啟動**:若 P2 已達需求,P3 不做也是合理結局——這正是 Plugin 架構的價值,不過度承諾未驗證的能力

---

## 風險管控

- 權限過濾雙層保障:查詢時 ES filter + 回傳前結果再過濾
- 第三方 API RPS 限制是最主要外部約束,Gateway 統一守門
- Graph 故障不影響向量搜尋;Reconciler 確保無任務靜悄悄消失
