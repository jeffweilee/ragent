# MCP Hub

MCP Hub 是一個將 REST API 動態轉換為 MCP Tool 的獨立微服務。只需撰寫 YAML 設定檔，無需修改任何程式碼，即可讓 LLM 透過 MCP 協定呼叫任意 HTTP API。

---

## 設計分層

```
┌──────────────────────────────────────────────────────┐
│  MCP Client (LLM / Claude / Agent)                   │
│  via Streamable HTTP  POST /mcp                      │
└────────────────────┬─────────────────────────────────┘
                     │ MCP JSON-RPC  (tools/call)
┌────────────────────▼─────────────────────────────────┐
│  AuthMiddleware                                       │
│  X-MCP-Hub-Token 驗證；/metrics 豁免                 │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────┐
│  HeaderForwardMiddleware                              │
│  將請求 headers 存入 ContextVar（_INCOMING_HEADERS） │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────┐
│  FastMCP  (FastAPI 核心)                              │
│  每個 MCP Tool 對應一個動態產生的 async callable     │
└────────────────────┬─────────────────────────────────┘
                     │  httpx.AsyncClient（per-system）
┌────────────────────▼─────────────────────────────────┐
│  Upstream REST API  (n 個獨立系統)                   │
└──────────────────────────────────────────────────────┘
```

### 模組職責

| 模組 | 檔案 | 職責 |
|------|------|------|
| **loader** | `mcp_hub.py` | 讀取 YAML → `_ToolSpec` / `_SystemSpec`；fault-isolated（一個系統壞不影響其他） |
| **render** | `_render.py` | 替換 `{% .Secrets.KEY %}` 佔位符；missing key 立即 fatal（CrashLoop visible） |
| **server** | `server.py` | ASGI 組合：FastMCP + AuthMiddleware + HeaderForwardMiddleware；uvicorn entrypoint |
| **doctor** | `doctor.py` | 靜態分析 YAML；CI 用，不需要真實 secret |
| **metrics** | `metrics.py` | Prometheus metrics（詳見下方） |
| **env** | `_env.py` | 環境變數讀取 helper（`str_env`、`int_env`、`bool_env`） |

### 啟動流程

```
build_mcp_app()
  │
  ├─ check_yaml(placeholder_ok=True)
  │   └─ 靜態分析：path placeholder 不符、非法 param name、GET body...
  │      靜態錯誤 → sys.exit(1)，LoadFailure（file parse/IO 錯誤）→ 繼續
  │
  ├─ build_hub(yaml_path, env=os.environ)
  │   └─ load_tools_yaml(strict=False)
  │       ├─ render_secrets()  ← missing secret → KeyError → CrashLoop
  │       ├─ yaml.safe_load()
  │       └─ _parse_tool() × N  ← 失敗 → LoadFailure，繼續下一個
  │
  ├─ 記錄 mcp_hub_tool_info / mcp_hub_system_up gauges（啟動即可見）
  ├─ log  mcp_hub.system_skipped（每個 LoadFailure）
  ├─ log  mcp_hub.config_ok
  │
  └─ build_app(bundle)  →  return ASGI app
```

### 執行時呼叫路徑

```
MCP tools/call  { name: "billing.list_charges", arguments: {...} }
  │
  └─ _call(**kwargs)              ← 動態產生的 async callable
      ├─ 讀取 _INCOMING_HEADERS ContextVar
      ├─ render forward_headers   ← 從 incoming 取值；缺少 → 跳過（不報錯）
      ├─ 組合 URL、query、body、headers
      ├─ httpx.AsyncClient.request()   ← per-system client，獨立 TCP pool
      ├─ 記錄 mcp_hub_tool_calls_total + mcp_hub_tool_call_duration_seconds
      └─ 回傳 { ok, status, data } 或 raise ToolError（JSON payload）
```

---

## 如何接入新的外部 API

### 1. 建立系統 YAML

在 `deploy/helm/mcp-hub/files/tools.d/` 新增一個 `<系統名稱>.yaml`：

```yaml
# 系統名稱（可省略，預設用檔名 stem）
system: payment

defaults:
  base_url: https://payment-api.internal
  timeout: 15.0             # 秒，預設 30.0
  max_connections: 20       # 每個系統獨立 TCP pool，預設 100
  headers:
    Accept: application/json

tools:
  - name: charge
    description: 為客戶建立一筆收費。
    method: POST
    path: /v1/charges
    static_headers:
      X-Api-Key: "{% .Secrets.PAYMENT_API_KEY %}"
    parameters:
      - name: amount
        type: integer
        location: body
        required: true
        description: 金額（分）。
      - name: currency
        type: string
        location: body
        required: false
        default: "usd"
```

Tool 名稱自動加前綴：`payment.charge`。

### 2. 設定 Vault Secret

在 `deploy/helm/mcp-hub/templates/vaultsecret.yaml` 加入新 key：

```yaml
keys:
  - MCP_HUB_AUTH_TOKEN
  - PAYMENT_API_KEY   # ← 新增
```

Pod 透過 `envFrom: secretRef` 讀取，`render_secrets()` 在啟動時替換 YAML 裡的佔位符。

### 3. CI 驗證（不需要真實 secret）

```bash
cd mcp_hub
uv run mcp-hub-doctor deploy/helm/mcp-hub/files/tools.d --placeholder-ok
```

`--placeholder-ok` 驗證：
- `base_url` 有無遺漏（相對路徑必須有）
- `{user_id}` 在 path 但沒有對應 `location: path` 參數
- GET/DELETE 上有 body 參數
- `{% .Secrets.lowercase_key %}` 等非法 key 格式
- `{{ }}` 和 Helm 模板語法衝突

### 4. Push → 自動重啟

```bash
git add deploy/helm/mcp-hub/files/tools.d/payment.yaml
git add deploy/helm/mcp-hub/templates/vaultsecret.yaml
git commit -m "add payment system MCP tools"
git push
```

ArgoCD 更新 ConfigMap → **stakater/reloader** 偵測到 annotation 自動重啟 Pod。**不需重新 build image，不需改任何程式碼。**

---

## 完整 YAML 欄位參考

### `defaults`（系統層級預設值）

| 欄位 | 類型 | 預設 | 說明 |
|------|------|------|------|
| `base_url` | string | — | 所有相對路徑的基底 URL |
| `timeout` | float | 30.0 | HTTP 逾時秒數 |
| `max_connections` | int | 100 | 此系統的 httpx 連線池上限 |
| `headers` | mapping | — | 套用到系統所有 Tool 的預設 headers |
| `verify_ssl` | bool | true | 關閉則跳過 TLS 驗證（必須是 yaml 布林，非字串 `"true"`） |

### `tools[]`

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `name` | string | ✓ | 工具名稱，結果為 `<system>.<name>` |
| `description` | string | — | 顯示給 LLM 的說明 |
| `method` | string | ✓ | GET / POST / PUT / PATCH / DELETE |
| `path` | string | ✓ | 相對路徑（需有 `base_url`）或絕對 URL |
| `base_url` | string | — | 覆蓋系統層 `base_url` |
| `timeout` | float | — | 覆蓋系統層 `timeout` |
| `body_format` | string | `json` | `json` / `form` / `multipart` |
| `static_headers` | mapping | — | 每次請求帶的固定 headers（可含 secret 佔位符） |
| `forward_headers` | mapping | — | 從 MCP 客戶端請求轉送的 headers |

### `parameters[]`

| 欄位 | 值 | 說明 |
|------|-----|------|
| `type` | `string` `integer` `number` `boolean` `array` `object` `file` | `file` = base64 字串，解碼後送 multipart |
| `location` | `query` `body` `path` `header` | 參數位置 |
| `required` | `true` / `false` | 必填/選填 |
| `default` | any | 選填參數預設值 |

### Headers 詳細說明

**`static_headers`** — 靜態值，每次請求都帶：
```yaml
static_headers:
  X-Api-Key: "{% .Secrets.MY_API_KEY %}"
  Content-Type: application/json
```

**`forward_headers`** — 從 MCP 客戶端請求中轉送，`{placeholder}` 是小寫 header 名稱：
```yaml
forward_headers:
  X-User-Id: "{x-user-id}"
  X-Context: "{x-user-id}@{x-tenant-id}"   # 多 placeholder 組合
```
任一 placeholder 缺少 → 整個 header 靜默跳過（不報錯）。

**Secrets 語法**：
```
{% .Secrets.MY_KEY %}
```
Key 必須符合 `[A-Z][A-Z0-9_]*`（大寫開頭）。禁止 `{{ }}`（和 Helm 衝突）。

---

## 完整範例

### GET + query parameter + secret header

```yaml
system: search

defaults:
  base_url: https://search.internal
  timeout: 10.0

tools:
  - name: find_products
    description: 依關鍵字搜尋產品。
    method: GET
    path: /v2/products
    static_headers:
      Authorization: "Bearer {% .Secrets.SEARCH_TOKEN %}"
    forward_headers:
      X-User-Id: "{x-user-id}"
    parameters:
      - name: q
        type: string
        location: query
        required: true
        description: 搜尋關鍵字。
      - name: limit
        type: integer
        location: query
        required: false
        default: 20
```

### POST + JSON body + path parameter

```yaml
  - name: get_order
    description: 取得訂單詳情。
    method: GET
    path: /v1/orders/{order_id}
    parameters:
      - name: order_id
        type: string
        location: path
        required: true

  - name: create_order
    description: 建立新訂單。
    method: POST
    path: /v1/orders
    body_format: json
    parameters:
      - name: items
        type: array
        location: body
        required: true
        description: 商品清單。
      - name: note
        type: string
        location: body
        required: false
```

### 檔案上傳（multipart）

```yaml
  - name: upload_doc
    description: 上傳文件（base64 編碼）。
    method: POST
    path: /v1/documents
    body_format: multipart
    parameters:
      - name: file
        type: file          # MCP 客戶端傳 base64 string；Hub 解碼後送 bytes
        location: body
        required: true
      - name: doc_type
        type: string
        location: body
        required: false
        default: "pdf"
```

---

## Metrics

### 完整 Metrics 一覽

| Metric | Type | Labels | 說明 |
|--------|------|--------|------|
| `mcp_hub_tool_info` | Gauge | `system`, `tool`, `method` | 啟動時為每個已註冊 Tool 設為 1；永遠 = 1。解決 zero-cardinality 問題 |
| `mcp_hub_system_up` | Gauge | `system` | 1 = 載入成功，0 = 載入失敗（file_parse 錯誤）。重啟後重設 |
| `mcp_hub_tool_load_failures_total` | Counter | `system`, `phase` | 啟動時載入錯誤累計數 |
| `mcp_hub_tool_calls_total` | Counter | `system`, `tool`, `outcome` | 每次工具呼叫的結果計數 |
| `mcp_hub_tool_call_duration_seconds` | Histogram | `system`, `outcome` | 上游呼叫延遲分佈（`tool` 刻意省略以控制基數） |

`phase` 值：`file_parse` / `tool_parse` / `registration`

`outcome` 值：`success` / `upstream_4xx` / `upstream_5xx` / `timeout` / `connect_error`

### 為何需要 Gauge（不只有 Counter）

- **Counter 重啟歸零**：每次 Pod 重啟，`mcp_hub_tool_load_failures_total` 從 0 開始。你無法用它判斷「現在這個 system 是否正常」。
- **Zero-cardinality 問題**：一個 Tool 從未被呼叫，`mcp_hub_tool_calls_total` 裡不存在它的 label。Alert rule 的 `absent()` 判斷就失效。
- **`mcp_hub_tool_info` 解法**：啟動即設值，Prometheus 立刻有所有 Tool 的 baseline。

### 常用 PromQL

**查看所有已註冊的 Tool（inventory）**
```promql
mcp_hub_tool_info == 1
```

**哪些 system 目前載入失敗**
```promql
mcp_hub_system_up == 0
```

**Alert：期望的 Tool 消失（ConfigMap 更新後 Tool 被移除）**
```promql
absent(mcp_hub_tool_info{system="billing", tool="billing.list_charges"})
```

**Alert：Tool 在過去 5 分鐘 error rate > 50%**
```promql
rate(mcp_hub_tool_calls_total{outcome!="success"}[5m])
  / rate(mcp_hub_tool_calls_total[5m]) > 0.5
```

**p95 延遲（按 system）**
```promql
histogram_quantile(0.95,
  rate(mcp_hub_tool_call_duration_seconds_bucket[5m])
)
```

**啟動失敗分佈（按 phase）**
```promql
increase(mcp_hub_tool_load_failures_total[1h])
```

---

## 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `MCP_HUB_TOOLS_YAML` | `tools.yaml` | YAML 檔案或目錄路徑 |
| `MCP_HUB_NAME` | `ragent-mcp-hub` | 對 MCP 客戶端的服務名稱 |
| `MCP_HUB_HOST` | `0.0.0.0` | 監聽位址 |
| `MCP_HUB_PORT` | `9000` | 監聽 port |
| `MCP_HUB_PATH` | `/mcp` | MCP Streamable HTTP 掛載路徑 |
| `MCP_HUB_STATELESS_HTTP` | `false` | Stateless HTTP 模式 |
| `MCP_HUB_JSON_RESPONSE` | `false` | JSON 回應模式（預設 SSE streaming） |
| `MCP_HUB_AUTH_HEADER` | `X-MCP-Hub-Token` | 認證 header 名稱 |
| `MCP_HUB_AUTH_TOKEN` | — | 認證 token；未設定 = 關閉認證（警告） |
| `LOG_LEVEL` | `INFO` | uvicorn log level |

---

## 目錄結構

```
mcp_hub/                      ← 獨立微服務根目錄
├── pyproject.toml            ← 獨立 Python 專案（mcp-hub）
├── Dockerfile                ← 生產映像
├── .env.example              ← 本機開發環境變數範本
├── SKILL.md                  ← Operator onboarding 快速指南
├── src/
│   └── mcp_hub/
│       ├── __init__.py
│       ├── _env.py           ← 環境變數 helper
│       ├── _render.py        ← Secret 佔位符渲染
│       ├── doctor.py         ← CI 靜態分析工具
│       ├── mcp_hub.py        ← YAML 載入 + Tool callable 產生
│       ├── metrics.py        ← Prometheus metrics
│       ├── server.py         ← ASGI 應用程式 + uvicorn entrypoint
│       └── tools.example.d/  ← 範例 YAML（httpbin、ragent）
└── tests/
    ├── unit/                 ← 快速單元測試（無 I/O）
    └── integration/          ← 端對端 HTTP 測試（pytest-httpserver）
```

## 本機開發

```bash
cd mcp_hub
cp .env.example .env
uv sync --dev

# 驗證 YAML（不需 secret）
uv run mcp-hub-doctor src/mcp_hub/tools.example.d --placeholder-ok

# 執行測試
uv run python -m pytest -q

# 啟動服務
MCP_HUB_TOOLS_YAML=src/mcp_hub/tools.example.d uv run python -m mcp_hub.server
```
