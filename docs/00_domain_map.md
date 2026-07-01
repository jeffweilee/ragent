# 00_domain_map.md — 模組領域邊界與 AI 任務護欄

> **用途**：此文件是 AI（Claude）在本專案執行任務的**主要導航地圖**。  
> 每次開始一個 Task 前，先定位「你要動哪個 Domain」，然後查閱該 Domain 的  
> 責任邊界、允許依賴、以及禁止跨越的規則。  
> 本文件與 `docs/00_rule.md`、`docs/00_spec.md` 共同構成行為契約；三者矛盾時以 `00_spec.md` 為準。

---
## 一、Domain 總覽

```
┌─────────────────────────────────────────────────────────────────────┐
│                         HTTP 邊界                                    │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │  Middleware  │  │   Routers    │  │  Schemas (Pydantic I/O)   │  │
│  └──────┬──────┘  └──────┬───────┘  └────────────┬──────────────┘  │
│         │                │                        │                  │
│  ┌──────▼────────────────▼────────────────────────▼──────────────┐  │
│  │                     Services                                   │  │
│  └──────────────────────────┬───────────────────────────────────┘  │
│                              │                                       │
│        ┌─────────────────────┼──────────────────────┐               │
│        ▼                     ▼                       ▼               │
│  ┌──────────┐   ┌─────────────────────┐   ┌──────────────────┐     │
│  │Repositories│  │     Pipelines       │   │     Extractors     │     │
│  └──────────┘   └────────┬────────────┘   └──────────────────┘     │
│                           │                                          │
│        ┌──────────────────┼─────────────┐                          │
│        ▼                  ▼             ▼                            │
│  ┌──────────┐  ┌──────────────┐  ┌──────────┐                      │
│  │  Storage  │  │   Clients    │  │  Errors   │                      │
│  └──────────┘  └──────────────┘  └──────────┘                      │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              Bootstrap (Composition Root)                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────┐   │
│  │  Auth     │  │ Utility  │  │ Security │  │  MCP Hub (process)│   │
│  └──────────┘  └──────────┘  └──────────┘  └───────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

  獨立 process（不掛載於 FastAPI app，不在上圖框內）：
  Workers (python -m ragent.worker)　／　Reconciler (python -m ragent.reconciler)
```

---
## 二、Domain 詳細邊界

### 2.1 Bootstrap（組合根）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/bootstrap/` |
| **責任** | 唯一 DI 接縫；讀取 env vars、構建所有外部依賴、組裝 Container dataclass、注入 Routers/Services/Workers。 |
| **對外暴露** | `Container` dataclass（供 `app.py`、worker、reconciler 使用）；`create_app()`。 |
| **允許依賴** | `utility/`、`clients/`、`repositories/`、`pipelines/`、`extractors/`、`services/`、`storage/`、`auth/`、`errors/`、`middleware/`。幾乎可引用所有 Domain，因為這裡是組裝層。 |
| **禁止事項** | ❌ Routers 不得反向依賴 bootstrap（循環）。❌ 除 `composition.py` 外，任何其他檔案不得直接讀取 env vars（`os.environ`）。❌ 不得持有長生命週期的 `AsyncConnection`。 |

**模組清單：**

| 檔案 | 用途 |
|---|---|
| `composition.py` | `build_container()` — 唯一配置組裝點；構建所有 singleton |
| `app.py` | `create_app()` — 掛載 routers、lifespan、middleware |
| `broker.py` | TaskIQ broker 工廠（standalone / sentinel）|
| `dispatcher.py` | 同步封裝層，讓同步呼叫能 enqueue async task |
| `init_schema.py` | DB + ES schema 初始化；`iter_statements` strip-then-split SQL parser |
| `logging_config.py` | structlog 設定、privacy denylist processor |
| `metrics.py` | Prometheus counter/histogram 定義 |
| `telemetry.py` | OTEL TracerProvider setup/shutdown |

---
### 2.2 Routers（表示層）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/routers/` |
| **責任** | HTTP 請求解析、Pydantic 校驗、呼叫 Services/Pipelines、回傳 HTTP 回應。**只做 I/O 轉換**。 |
| **對外暴露** | `APIRouter` 實例，由 `bootstrap/app.py` 掛載。 |
| **允許依賴** | `schemas/`、`services/`、`errors/`、`auth/deps.py`、`clients/rate_limiter.py`（rate limit）、`middleware/`（間接，透過 request.scope）。 |
| **禁止事項** | ❌ 不得直接呼叫 `repositories/`（繞過 service 層）。❌ 不得含業務邏輯（計算、狀態判斷）。❌ 不得讀取 `os.environ`。❌ 不得用 `Header(alias="X-User-Id")` — 必須用 `Depends(get_user_id)`。❌ 不得在 decorator 上寫完整路徑（版本必須在 `APIRouter(prefix=...)` 上宣告）。 |

**模組清單：**

| 檔案 | 路由前綴 | 核心職責 |
|---|---|---|
| `ingest.py` | `/ingest/v1` | Create / Read / List / Delete / Rerun / Upload |
| `chat.py` | `/chat/v1` | 同步聊天、SSE 串流 |
| `retrieve.py` | `/retrieve/v1` | 無 LLM 純檢索 |
| `chatagent.py` | `/chatagent/v1` | ChatAgent 上游代理(POST + sessionList / session GET/PUT/DELETE)|
| `chatagent_v2.py` | `/chatagent/v2` | ChatAgent raw-proxy(原樣轉發上游 payload,串流/非串流)|
| `chatagent_v3.py` | `/chatagent/v3` | twp-ai protocol 代理(`RunAgentInput` → AG-UI SSE)+ v3 session 管理(twp-ai message shape)。POST handler 依賴注入的 `agent_factory: AgentFactory`（`Callable[[user_id, user_token], twp_ai.agent.Agent]`），**不**直接 import `ADKAgent`/`ADKCaller` 等具體類別（T-CAv3.DIP）。 |
| `_chatagent_proxy.py` | —(共用 helper)| v1/v3 session 路由共用的 proxy_get / proxy_write 轉發與 timeout→504 / error→502 映射 |
| `feedback.py` | `/feedback/v1` | 使用者回饋 HMAC token 驗證與雙寫 |
| `mcp.py` | `/mcp/v1` | JSON-RPC 2.0 MCP Tool Server（P2.5）|
| `skill.py` | `/skills/v1` | 使用者 skill preset CRUD（owner-scoped；T-SK）|
| `mcp_tools/` | —(tool 描述子)| 每個 sub-module 定義一個 MCP tool 的 input model / inputSchema / Tool descriptor |
| `admin_embedding.py` | `/embedding/v1` | embedding model 生命週期管理（B50；promote/cutover/rollback/commit/abort/state）|
| `admin_ingest.py` | `/ingest/v1/upload` | multipart 上傳路由（direct route；no `APIRouter` prefix）|
| `attachments.py` | `/chatagent/v3/attachments` | `POST /upload`(MIME/size 驗證 → `service.upload()` 快速 intake，202)/ `GET ?threadId=`(列出該對話的 attachments，含 `errorCode`/`errorReason`)/ `GET /{attachmentId}`(輪詢單筆狀態；不存在回 404 `ATTACHMENT_NOT_FOUND`，T-CAT.W2)；獨立檔案但與 `chatagent_v3.py` 共用 `/chatagent/v3` 路徑空間（同 `admin_ingest.py` 與 `ingest.py` 共用 `/ingest/v1` 的既有模式），滿足 `tests/unit/test_api_versioning.py` 的版本前綴規則 |
| `admin_ops.py` | `/ops/v1` | 維運操作(retry)|
| `health.py` | `/livez`, `/readyz`, `/startupz`, `/metrics` | 健康探針、Prometheus 指標 |
| `health_probes.py` | —(probe 實作)| `/readyz` 的 MariaDB / ES / Redis / MinIO probe 實作,由 `health.py` 注入 |

> 另有 `/twp/v1` router 由 `packages/twp-ai`(repo 內獨立 package)提供,於 `bootstrap/app.py` 掛載;`/chatagent/v3` 依賴該 package 的 `twp_ai.agent.Agent` Protocol 與 schemas,具體實作(`ADKAgent` + ragent 端 `clients/adk_caller.py`)由 `bootstrap/composition.py::_build_chatagent_agent_factory()` 組裝成 `agent_factory` 後注入,router 本身不 import 具體類別。詳見 `docs/chatagent_agent_backend.md`。

---
### 2.3 Services（業務邏輯層）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/services/` |
| **責任** | 封裝業務流程協調；不直接操作 DB，透過 Repositories；不處理 HTTP 細節。 |
| **允許依賴** | `repositories/`、`storage/`、`clients/`、`errors/`、`schemas/`（輸入 DTO）、`utility/`。 |
| **禁止事項** | ❌ 不得直接操作 HTTP request/response 物件。❌ 不得直接執行 SQL（必須透過 repositories）。❌ 不得讀取 `os.environ`。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `ingest_service.py` | inline / file / upload ingest 流程協調；supersede 觸發；delete cascade 協調 |
| `skill_service.py` | 使用者 skill preset 業務邏輯（owner-scoped CRUD + typed errors）；merge 內建 preset；`resolve_instructions` 供 `/chatagent/v3` 注入（T-SK）|
| `skill_presets.py` | 內建唯讀 preset skill registry（skill-manager;`PRESETS`/`PRESET_BY_ID`/`PRESET_NAMES_CASEFOLD`）（T-SK）|
| `chatagent_session.py` | ChatAgent session payload 轉形:sessionName 的機器情境 wrapper strip、`node_to_role` 角色映射、v3 message reshape(`{id, role, content}`)|
| `embedding/registry.py` | 活躍 embedding model config 快取（從 DB 讀取；`refresh()` 在 lifespan 呼叫）|
| `embedding/lifecycle.py` | embedding model 狀態機：draft → staging → active → retired（B50）|
| `embedding/backfill.py` | backfill 長跑背景 op（enqueue 給 worker）|
| `embedding/preflight.py` | embedding cutover 前置檢查：warmup + similarity gate |
| `chat_attachment_service.py` | `upload()`(快速 intake，同步)：`DocumentStore.put`(raw bytes) → `attachment_repository.create()`(UPLOADED) → `dispatcher.enqueue("attachment.process", ...)`。`process()`(worker 呼叫，異步，T-CAT.W2)：`attachment_repository.claim_for_processing()`(UPLOADED→PROCESSING)→ 取回 raw bytes → `ChatAttachmentPipeline.run()` → `ASTCipher.encrypt_ast()` → `DocumentStore.put`(每個 artifact)→ `add_artifact` → `update_status(READY)`；例外時 terminalize 為 FAILED（`error_code`/`error_reason`），不 re-raise |
| `document_artifact_resolver.py` | `attachment_ids` → `ContextItems`：`DocumentStore.get` → `ASTCipher.decrypt_ast` → 組裝給 `/chatagent/v3` 的 `<attachments>` 區塊 |

---
### 2.4 Repositories（資料持久層）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/repositories/` |
| **責任** | 專職 DB CRUD。每個 public method 接受 `AsyncEngine` 或 `AsyncConnection`（每次呼叫自行 checkout）。 |
| **允許依賴** | `utility/datetime.py`（UTC 補丁）、`errors/`（自定義 exception）、`schemas/`（映射 DTO）。 |
| **禁止事項** | ❌ 不得含業務邏輯（只做 CRUD + 簡單 WHERE 條件）。❌ 不得持有長生命週期 `Connection`（module / class 層級）。❌ 不得定義 FK constraint（應用層維護關聯性）。❌ 不得跨 Domain 依賴 `services/` 或 `pipelines/`。 |

**模組清單：**

| 檔案 | 管理的資料 |
|---|---|
| `document_repository.py` | `documents` 表 — CRUD、status 轉換、選舉（supersede）|
| `feedback_repository.py` | `feedback` 表 — 投票記錄寫入 |
| `system_settings_repository.py` | `system_settings` 表 — embedding model config 讀寫 |
| `attachment_repository.py` | `chat_attachments` + `chat_attachment_artifacts` 表 — CRUD、依 `thread_id`/`create_user` 查詢；`claim_for_processing()` 原子 claim(UPLOADED→PROCESSING，鏡像 `DocumentRepository._atomic_claim`)；`update_status()` 可選寫入 `error_code`/`error_reason`(T-CAT.W2) |
| `skill_repository.py` | `skills` 表 — owner-scoped CRUD（每條語句都以 `user_id` 過濾；T-SK）|

---
### 2.5 Pipelines（Haystack 管線）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/pipelines/` |
| **責任** | 定義並建構 Haystack Pipeline 物件；不主動觸發執行（執行由 worker task 呼叫 `pipeline.run()`）。 |
| **允許依賴** | `clients/`（EmbeddingClient）、`utility/`、`errors/`、`schemas/`。**不得依賴** `repositories/`、`services/`、`routers/`。 |
| **禁止事項** | ❌ 不得在 `pipeline.run()` 外執行 DB 操作。❌ 不得假設 Haystack component kwargs 存在 — 必須用 `inspect.signature` 驗證並加 `# verified against haystack-elasticsearch X.Y.Z` 注解。❌ 不得在 pipeline 內直接讀取 env vars。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `ingest/__init__.py` | `build_ingest_pipeline()` — 公用介面；re-exports 所有 sub-module 符號 |
| `ingest/loader.py` | `_TextLoader`、`ALLOWED_MIMES` |
| `ingest/splitter.py` | `_MimeAwareSplitter`、`_MarkdownASTSplitter`、`_HtmlASTSplitter`、`_DocxASTSplitter`、`_PptxASTSplitter`、`_PdfASTSplitter`、`_CsvASTSplitter`、`INGEST_PDF_MARGIN_PTS` |
| `ingest/chunker.py` | `_BudgetChunker`、`_pack_atoms`、`validate_chunk_config`、`CHUNK_TARGET_CHARS`、`CHUNK_MAX_CHARS`、`CHUNK_OVERLAP_CHARS`、`CHUNK_MAX_PIECES_PER_ATOM` |
| `ingest/embedder.py` | `_DocumentEmbedder` |
| `chat_attachment/pipeline.py` | `ChatAttachmentPipeline` — load → optional unprotect(白名單見 §2.7) → AST build；**不**加密、**不**持久化（SRP：留給 service 層）|
| `chat_attachment/ast_builder.py` | `CompleteAST`、`SimplifiedAST` — 複用 `ingest/splitter.py` 的 `_MimeAwareSplitter` 家族，不重寫格式解析 |
| `retrieve/__init__.py` | `build_retrieval_pipeline()`、`run_retrieval()` — 公用介面；re-exports 所有 sub-module 符號 |
| `retrieve/_constants.py` | `DEFAULT_TOP_K`、`DEFAULT_MIN_SCORE`、`MAX_TOP_K`、`EXCERPT_MAX_CHARS_DEFAULT`、`_VALID_MODES` |
| `retrieve/joiner.py` | `build_es_filters`、`dedupe_by_document`、`doc_to_source_entry` |
| `retrieve/query_embedder.py` | `_QueryEmbedder`、`_DynamicFieldEmbeddingRetriever` |
| `retrieve/hydrator.py` | `_SourceHydrator`、`_Reranker`、`_LLMGenerator`、`_ExcerptTruncator` |
| `retrieve/retriever.py` | `_FeedbackMemoryRetriever` |
| `observability.py` | `wrap_pipeline_component()` — 每個 Haystack component 的 structlog + OTEL 雙發射封裝 |

---
### 2.6 Extractors（可插拔萃取器）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/extractors/` |
| **責任** | 實作 `ExtractorPlugin` Protocol；`PluginRegistry` 管理 fan-out 生命週期。（類別名稱保持不變；目錄從 `plugins/` 改名為 `extractors/`。）|
| **允許依賴** | `repositories/`（透過 constructor injection）、`clients/`（透過 constructor injection）、`errors/`。 |
| **禁止事項** | ❌ Extractors **不得** `import` `pipelines/` 或任何 HTTP 層。❌ 不得在 `extract()` 或 `delete()` 內持有 DB transaction（fan_out 在 TX 外執行）。❌ `registry.register()` 之後不得修改 extractor 實例。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `protocol.py` | `ExtractorPlugin` Protocol 定義（frozen v1）|
| `registry.py` | `PluginRegistry` — register、fan_out（60s timeout）、fan_out_delete |
| `vector.py` | `VectorExtractor` — ES bulk write（required extractor）|
| `stub_graph.py` | `StubGraphExtractor` — no-op（optional，P3 佔位）|

---
### 2.7 Clients（外部服務客戶端）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/clients/` |
| **責任** | 封裝對 Embedding / LLM / Rerank / Redis rate-limiter 的 HTTP 呼叫；retry、timeout、error mapping。 |
| **允許依賴** | `errors/`、`utility/`；shared `httpx.Client`（由 bootstrap 注入）。 |
| **禁止事項** | ❌ 不得硬編碼 URL（由 bootstrap 讀取 env 注入）。❌ 不得持有多個 `httpx.Client` 實例（共用 bootstrap 的 `http`）。❌ 禁止使用 `with self._http.post(...) as resp:`（應統一使用 `resp = self._http.post(...); resp.raise_for_status()` 模式）。❌ 不得讀取 `os.environ`。 |

**模組清單：**

| 檔案 | 對應外部服務 |
|---|---|
| `auth.py` | `TokenManager` — J1/J2 token exchange，single-flight refresh |
| `embedding.py` | `EmbeddingClient` — batch embed（32 chunks/call），30s timeout |
| `llm.py` | `LLMClient` — chat + stream，token budget |
| `rerank.py` | `RerankClient` — 重排，fail-open on 5xx（C4）|
| `rate_limiter.py` | `RateLimiter` — Redis fixed-window per user |
| `adk_caller.py` | `ADKCaller` — `/chatagent/v3` twp-ai run 的上游代理 backend(`RunAgentInput` → v2 wire → `UpstreamMessage` stream)。由 `bootstrap/composition.py::_build_chatagent_agent_factory()` 組裝注入,router 不再直接 import。|
| `embedding_model_config.py` | embedding model identity 設定(B50);ES `dense_vector.dims` 界限於 boot 驗證 |
| `unprotect.py` | `UnprotectClient` — 外部 unprotect API 取回原始 binary(T-UP.3)|
| `chat_stream_store.py` | `ChatStreamStore`(T-CAv3R) — Redis Stream tee/replay，讓 `/chatagent/v3` SSE run 可斷線重連 |

---
### 2.8 Schemas（I/O DTO）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/schemas/` |
| **責任** | Pydantic v2 model：HTTP request / response 的形狀定義與欄位驗證。純資料結構，無業務邏輯。 |
| **允許依賴** | Python stdlib、Pydantic。**不得** import 任何 ragent 業務模組。 |
| **禁止事項** | ❌ Schemas 不得含業務判斷（if/else 邏輯）。❌ 不得直接操作 DB 或外部服務。❌ 不得讀取 `os.environ`。 |

主要檔案：
- `ingest.py`（InlineIngestRequest / FileIngestRequest / IngestCreatedResponse / IngestListItem / IngestListResponse / IngestDetailResponse）
- `retrieve.py`（RetrieveRequest / ChunkEntry / RetrieveResponse）
- `embedding.py`（PromoteRequest / CutoverRequest）
- `chat.py`（ChatRequest / ChatResponse / Source / StreamDelta/Done/Error）
- `chatagent.py`(SessionRenameRequest / SessionDeleteRequest)
- `attachments.py`(`AttachmentMime` enum、`UNPROTECT_MIMES` frozenset、AttachmentUploadResponse / AttachmentListResponse)
- `feedback.py`（FeedbackRequest / vote / reason enum）
- `skill.py`（SkillWriteRequest / SkillResponse / SkillListResponse；T-SK）
- `_common.py`(source_app / source_meta 共用 filter 欄位驗證)

---
### 2.9 Storage（物件儲存）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/storage/` |
| **責任** | MinIO 操作封裝（GET / PUT / HEAD / DELETE）與多站點注冊表。 |
| **允許依賴** | `errors/`、`utility/`。 |
| **禁止事項** | ❌ MinIO 物件在 READY / DELETE 後不得刪除（audit/replay 保留）— 規則在 `00_spec.md §3.1`。❌ 不得讀取 `os.environ`（site config 由 bootstrap 注入）。 |

主要檔案：`minio_client.py`（`MinIOClient` — 舊版 S3 操作封裝，未被 composition root 接線，與本計畫無關，保留供既有單元測試）、`minio_registry.py`（`MinioSiteRegistry` — 多 site 查詢，ingest 與 chat-attachment 共用的唯一生產路徑；`get_object`/`delete_object`/`stat_object` 已接受呼叫端自訂 key，`put_object_default` 才是 ingest 專屬鍵格式）、`document_store.py`（`DocumentStore` Protocol — `put`/`get`/`delete`/`exists`；DIP 邊界，service 層只依賴此介面，不直接 import `minio_registry.py`）、`minio_document_store.py`（`MinIODocumentStore` — `DocumentStore` 的唯一實作，建構時注入既有的 `MinioSiteRegistry`，使用 `default()` site；`get`/`delete`/`exists` 直接呼叫 registry 既有方法，`put` 呼叫 registry 新增的通用 `put_object(site, key, ...)` — 與 `put_object_default` 共用同一段 S3 呼叫邏輯，差異只在 key 由呼叫端提供）。

---
### 2.10 Auth（認證）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/auth/` |
| **責任** | JWT 驗證（joserfc）、`user_id` 解析、FastAPI `Depends` 注入。 |
| **允許依賴** | `errors/`、`utility/`。 |
| **禁止事項** | ❌ 不得在 auth 層做授權（permission check）— 授權屬 OpenFGA P2 範疇。❌ 不得在 route handler 中直接讀 `Header(alias="X-User-Id")` — 必須 `Depends(get_user_id)`。❌ `VerifyingTokenManager`（JWT 驗證）與 `TokenManager`（J1/J2 API token）是完全不同的類別，不得混用。 |

主要檔案：`jwt.py`（VerifyingTokenManager — JWKS + joserfc 驗簽）、`deps.py`（get_user_id FastAPI Depends）。

---
### 2.11 Middleware（HTTP 中介層）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/middleware/` |
| **責任** | 每個 HTTP 請求的橫切關注（logging、user_id 注入）；TaskIQ context 傳遞。 |
| **允許依賴** | `errors/`、`utility/`。 |
| **禁止事項** | ❌ 不得含業務邏輯。❌ Middleware 不得依賴 `services/` 或 `repositories/`。 |

主要檔案：`logging.py`（RequestLoggingMiddleware — api.request/error；user_id 寫入 scope）、`taskiq_context.py`（StructlogContextMiddleware）。

---
### 2.12 Errors（錯誤類型）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/errors/` |
| **責任** | 所有業務異常的定義（`error_code` + `http_status`）；RFC 9457 Problem Details 格式化。 |
| **允許依賴** | Python stdlib 只。 |
| **禁止事項** | ❌ 新增錯誤碼必須同時更新 `docs/00_spec.md §4.1.2`。❌ 不得在 errors/ 內 import 任何 ragent 業務模組。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `codes.py` | 所有 `error_code` 字串常數 |
| `problem.py` | RFC 9457 Problem Details 格式化、`error_code` → `type` URI mapping |
| `upstream.py` | `UpstreamServiceError`（502）、`UpstreamTimeoutError`（504）基礎類別 |

---
### 2.13 Utility（橫切工具）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/utility/` |
| **責任** | 純函數工具；無狀態；無外部依賴。 |
| **允許依賴** | Python stdlib 只。 |
| **禁止事項** | ❌ 不得放業務邏輯（只放可被任何 domain 引用的純工具）。❌ 不得讀取 `os.environ`（env 工具除外，本身就是封裝 env 讀取）。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `env.py` | `require()`, `int_env()`, `bool_env()`, `optional_str_env()` — env 讀取工具；空字串 `""` 視同 `None` |
| `datetime.py` | UTC datetime 工具、naive datetime → aware 補丁 |
| `compat.py` | Python 版本相容性 shim |
| `embedding_lifecycle.py` | embedding model 生命週期工具函數（純計算）|
| `feedback_token.py` | HMAC feedback token 生成與驗證 |

---
### 2.14 Security（安全工具）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/security/` |
| **責任** | 檔案安全校驗（zip bomb / path traversal）；金鑰管理與對稱加密（KEK/DEK、AES-256-GCM）。 |
| **允許依賴** | Python stdlib；`cryptography`（業界標準加密函式庫，僅此 domain 可引用）。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `archive_guard.py` | DOCX / PPTX zip preflight — members、ratio、expanded bytes 檢查（`INGEST_MAX_ARCHIVE_MEMBERS` / `_RATIO` / `_EXPANDED_BYTES`）|
| `key_manager.py` | `KeyManager` — 啟動時用 `RAGENT_KEK_BASE64` 解開 `RAGENT_ENCRYPTED_DEK_BASE64`，process 生命週期持有單一 DEK（ISP：對外只曝露 `.dek`，不洩漏 KEK/wrap 細節）|
| `ast_cipher.py` | `ASTCipher` — AES-256-GCM，依賴 `KeyManager.dek`；`encrypt_ast()` / `decrypt_ast()` |

---
### 2.15 Workers（TaskIQ Task Entrypoints）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/workers/` |
| **責任** | `@broker.task` 定義；worker process（`python -m ragent.worker`）的進入點，承接 `ingest.pipeline`／`ingest.backfill_candidate`／heartbeat。Task body 呼叫 `pipelines/`、`repositories/`、`services/`，不含自身業務規則之外的邏輯。 |
| **允許依賴** | `bootstrap/`（`broker`、`metrics`、共用 `Container`）、`pipelines/`、`repositories/`、`services/`、`schemas/`、`errors/`、`utility/`。 |
| **禁止事項** | ❌ 不得在 task body 內跨 external call 持有 DB transaction（同 `00_spec.md §3.1` locking）。❌ 不得被 `routers/`、`services/` 直接 import（只能透過 `kiq()` 派送）。❌ 不得讀取 `os.environ`。 |

**模組清單：**

| 檔案 | 職責 |
|---|---|
| `ingest.py` | `ingest.pipeline` task — TX-A claim → pipeline body（pipelines/ingest）→ TX-B 終態；`ingest.supersede` 選舉（呼叫 `services/ingest_service.IngestService`） |
| `backfill.py` | `ingest.backfill_candidate` task（T-EM-R.9）— scroll stable_index、補嵌入到 candidate_index |
| `heartbeat.py` | PENDING row 30s heartbeat 背景迴圈 |
| `attachment.py` | `attachment.process` task（T-CAT.W2）— 呼叫 `services/chat_attachment_service.ChatAttachmentService.process()`；`RAGENT_KEK_BASE64` 未設定時 no-op + log |

---
### 2.16 Reconciler（獨立 Process）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/reconciler.py` |
| **責任** | K8s CronJob；掃描 stale UPLOADED / PENDING / DELETING，re-kiq 或 FAILED；multi-READY 修復（R4）。 |
| **允許依賴** | `repositories/`、`bootstrap/`（共用 Container）、`errors/`、`utility/`。 |
| **禁止事項** | ❌ 不得引入 TaskIQ `@broker.task` 定義（只呼叫 `kiq()`）。❌ 不得持有 DB transaction 跨 plugin / external call 邊界。❌ 必須以 `SELECT … FOR UPDATE SKIP LOCKED` 避免多 instance 競爭。 |

---
### 2.17 MCP Hub（獨立 Process）

| 項目 | 說明 |
|---|---|
| **路徑** | `src/ragent/mcp_hub/` |
| **責任** | 獨立 FastMCP 服務；從 `tools.yaml` 動態載入第三方 REST API 工具。 |
| **允許依賴** | `utility/`、`errors/`。**完全獨立**於 ragent 主服務。 |
| **禁止事項** | ❌ 不得與 ragent main process 共用任何 singleton。❌ 不得依賴 `bootstrap/composition.py`。 |

---
## 三、依賴方向規則（AI 操作前必讀）

```
允許的依賴方向（→ 表示「可以 import」）：

Routers     → Services, Schemas, Errors, auth/deps, clients/rate_limiter
Services    → Repositories, Storage, Clients, Errors, Schemas, Utility
Repositories→ Utility, Errors, Schemas
Pipelines   → Clients, Utility, Errors, Schemas
Extractors  → Repositories(注入), Clients(注入), Errors
Clients     → Errors, Utility
Storage     → Errors, Utility
Auth        → Errors, Utility
Middleware  → Errors, Utility
Schemas     → (stdlib + Pydantic only)
Errors      → (stdlib only)
Utility     → (stdlib only)
Security    → stdlib, cryptography（唯一允許的第三方依賴；其餘 domain 不得直接 import cryptography）
Bootstrap   → 全部（唯一可以組裝所有層的地方）
Workers     → Bootstrap(broker/metrics/Container), Pipelines, Repositories, Services, Schemas, Errors, Utility
Reconciler  → Repositories, Bootstrap(Container), Errors, Utility
MCP Hub     → Utility, Errors（完全獨立 subprocess）

Routers → AgentFactory（`twp_ai.agent.Agent` Protocol 的型別別名，由 Bootstrap 注入的 callable）（✅，T-CAv3.DIP）
Bootstrap → Agent/Caller 的具體類別（組裝 factory closure，如 `_build_chatagent_agent_factory()`）（✅，Composition Root 是唯一允許依賴具體實作的層）

❌ 禁止反向依賴：
  Repositories → Services（❌）
  Pipelines    → Services 或 Repositories（❌）
  Extractors   → Pipelines 或 Routers（❌）
  Clients      → Repositories 或 Services（❌）
  Schemas      → 任何 ragent 業務模組（❌）
  Errors       → 任何 ragent 業務模組（❌）
  Utility      → 任何 ragent 業務模組（❌）
  Routers      → Agent/Caller 的具體類別（❌；曾是 `chatagent_v3.py` 的違規模式，已於 T-CAv3.DIP 重構移除）
```

---
## 四、AI 任務執行護欄（Harness Rules）

### R1：Task 定位 — 先問「我在哪個 Domain？」

在開始任何修改前，回答以下問題：

1. **我要改的行為屬於哪個 Domain？**（參考第二節）
2. **這個 Domain 允許依賴哪些模組？**（參考第三節）
3. **有沒有任何 `docs/00_rule.md` 中的規則適用於這個 Domain？**

如果不確定，先查 `docs/00_spec.md` 確認行為規格，再動手。

---
### R2：新檔案 — 放到正確 Domain

| 要新增的類型 | 放在哪裡 |
|---|---|
| 新 API endpoint | `routers/` + `schemas/` + unit test in `tests/unit/` |
| 新業務邏輯（非 CRUD） | `services/` |
| 新 DB 操作 | `repositories/` |
| 新 Haystack component | `pipelines/ingest/` 或 `pipelines/retrieve/` |
| 新外部 API 客戶端 | `clients/` |
| 新純工具函數 | `utility/` |
| 新 env var 讀取 | 只在 `bootstrap/composition.py` |
| 新錯誤碼 | `errors/codes.py` + `docs/00_spec.md §4.1.2` 同 commit |
| 新 Prometheus metric | `bootstrap/metrics.py` |
| 新 MinIO site | `storage/minio_registry.py` |

---
### R3：禁止清單（任何 Domain 通用）

| ❌ 禁止行為 | 正確做法 |
|---|---|
| 在 `routers/` 寫業務邏輯 | 移到 `services/` |
| 在 `services/` 直接執行 SQL | 透過 `repositories/` |
| 在 `repositories/` 含業務判斷 | 移到 `services/` |
| 在 `pipelines/` import `repositories/` | pipeline 透過 constructor injection 接受依賴 |
| 在 router handler 讀 `Header(alias="X-User-Id")` | `Depends(get_user_id)` |
| 任意地方讀 `os.environ`（除了 `utility/env.py` 和 `bootstrap/composition.py`）| 全部集中到 `bootstrap/composition.py` |
| 在非 `bootstrap/` 模組持有長生命週期 DB connection | 改為 engine.begin()/connect() per call |
| 新 error_code 不加到 spec | 同 commit 更新 `docs/00_spec.md §4.1.2` |
| mock Haystack component 不用 `autospec=True` | 加 `spec=ComponentClass` 或 `autospec=True` |
| `structlog` 測試用 `caplog` bridge | 改用 `structlog.testing.capture_logs()` |
| 在測試中 `asyncio.create_task(server.serve())` | 改用 `subprocess.Popen` 隔離進程 |
| `with self._http.post(...) as resp:` | `resp = self._http.post(...); resp.raise_for_status()` |

---
### R4：狀態機規則（Ingest Domain 特有）

```
合法轉換：
  UPLOADED → PENDING  （worker atomic claim）
  PENDING  → READY    （pipeline 成功，含 supersede 選舉）
  PENDING  → FAILED   （pipeline 失敗 / reconciler > 5 attempts）
  READY    → DELETING （DELETE API）
  PENDING  → DELETING （DELETE API 搶先）
  DELETING → (row deleted) （cascade 完成）

禁止轉換：
  ❌ READY → PENDING（READY 只能重新 POST 觸發 supersede）
  ❌ FAILED → 任何（只能重新 POST 或用 /rerun）
  ❌ DELETING → 任何（DELETING 是 terminal 前的 transient）
```

Supersede 選舉規則：**同一 `(source_id, source_app)` 最多一個 READY**。轉為 READY 前，用 `SELECT MAX(created_at) FOR UPDATE` 選出 survivor；落敗者自降 PENDING → DELETING。

---
### R5：日誌規則摘要（所有 Domain）

每個 public service method、每個 task、每個跨進程邊界，**必須有進出日誌**：

```python
# 進入
log.info("ingest.started", document_id=doc_id, user_id=user_id)

# 成功退出
log.info("ingest.ready", document_id=doc_id, duration_ms=elapsed, chunks_total=n)

# 失敗退出
log.error("ingest.failed", document_id=doc_id, error_code="EMBEDDER_ERROR")
```

**禁止記錄**：`query`、`prompt`、`messages`、`completion`、`chunks`、`embedding`、`documents`、`body`、`authorization`、`cookie`、`password`、`token`、`secret`。

---
### R6：測試層級規則

| 測試類型 | 放在哪 | 規則 |
|---|---|---|
| Unit（< 1s，mock 外部） | `tests/unit/` | 必須用 `autospec=True`；structlog 用 `capture_logs()` |
| Integration（testcontainers）| `tests/integration/` | 必須標記 `@pytest.mark.docker`；不得 skip |
| E2E（完整 stack）| `tests/e2e/` | Release step only；不在 per-commit gate |

**新 behavior 必須先有 failing test（Red）才能寫 production code（Green）— TDD 強制。**

---
### R7：Commit 前置條件

1. `make format` ✅  
2. `make lint` ✅  
3. `make test-gate`（含 `@pytest.mark.docker`）✅ — 0 skipped  
4. `uv run bandit -r src/ --severity-level high` ✅  
5. `/simplify` skill 已執行 ✅  
6. `/review` skill 已執行 ✅  
7. `.claude/.pre_commit_approved` 由 hook 寫入（非手動）✅  

---
## 五、快速查詢索引

> 新功能：查 `docs/00_spec.md` → `docs/00_plan.md` → 定位 Domain（§二）→ Red test → Green impl → `/simplify` + `/review` + commit。

### 「我改動了 X，可能影響哪些 Domain？」

| 改動 X | 可能影響的 Domain |
|---|---|
| `repositories/document_repository.py` | Services（ingest_service）、Extractors（VectorExtractor）、Reconciler |
| `clients/embedding.py` | Pipelines（ingest）、Extractors（VectorExtractor）|
| `bootstrap/composition.py` | 所有 Domain（DI 變更）|
| `schemas/ingest.py` | Routers（ingest）、Services（ingest_service）|
| `errors/codes.py` | 所有 Domain + `docs/00_spec.md §4.1.2` |
| `pipelines/retrieve/__init__.py` | Routers（chat、retrieve）、integration tests |
| `bootstrap/metrics.py` | 所有有 metric emit 的 Domain |
| `security/key_manager.py` | Bootstrap（建構順序）、`security/ast_cipher.py`、`services/chat_attachment_service.py`、`services/document_artifact_resolver.py` |
| `storage/document_store.py` | Bootstrap、`services/chat_attachment_service.py`、`services/document_artifact_resolver.py`（DIP：兩者只 import Protocol，不 import `minio_document_store.py`）|

