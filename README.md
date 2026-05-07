# ragent

Enterprise internal knowledge retrieval backend — streaming RAG answers grounded in private documents.

**Phase 1:** Ingest CRUD + Indexing Pipeline + Chat (non-streaming & SSE). Auth disabled (`X-User-Id` header trusted, audit only).

**Phase 1 v2 (2026-05-06):** Ingest API replaced with JSON-only `POST /ingest` (no multipart); discriminator `ingest_type ∈ {inline, file}` selects in-body content vs. caller-owned MinIO object; MIME allow-list trimmed to `{text/plain, text/markdown, text/html}`; pipeline split into MIME-aware AST splitters (mistletoe, selectolax) feeding a single mime-agnostic char-budget chunker (1000/1500/100); ES `chunks_v1` adds a `raw_content` field (`_source`-only) so chat citations and LLM context render the original markdown/HTML faithfully. Chunks live only in ES — the MariaDB `chunks` table is dropped. See `docs/team/2026_05_06_ingest_api_v2.md`.

---

## Quick Start

### Prerequisites

| Service | Version |
|---|---|
| Python | ≥ 3.12 |
| uv | latest |
| MariaDB | 10.6 |
| Redis Sentinel | — |
| Elasticsearch | 9.2.3 |
| MinIO | any |

### 1. Install dependencies

```bash
uv sync
```

### 2. Set environment variables

```bash
# Required
export MARIADB_DSN="mysql+pymysql://user:pass@localhost:3306/ragent"
export REDIS_MODE=sentinel
export REDIS_SENTINEL_HOSTS="localhost:26379"
export REDIS_BROKER_SENTINEL_MASTER="mymaster"
export ES_HOSTS="http://localhost:9200"
export MINIO_SITES='[{"name":"__default__","endpoint":"localhost:9000","access_key":"minioadmin","secret_key":"minioadmin","bucket":"ragent","secure":false}]'
# Add additional sites for ingest_type=file from caller-owned buckets:
# export MINIO_SITES='[{"name":"__default__",...}, {"name":"tenant-eu-1","endpoint":"eu.example:9000","access_key":"...","secret_key":"...","bucket":"tenant-eu","secure":true,"read_only":true}]'

# P1 open mode (required)
export RAGENT_AUTH_DISABLED=true
export RAGENT_ENV=dev

# Third-party API clients
export EMBEDDING_API_URL="http://embedding-service/embed"
export LLM_API_URL="http://llm-service/chat"
export RERANK_API_URL="http://rerank-service/rerank"

# Optional tuning
export CHAT_JOIN_MODE="rrf"
export EXCERPT_MAX_CHARS=512
export WORKER_HEARTBEAT_INTERVAL_SECONDS=30
export PIPELINE_TIMEOUT_SECONDS=1800
export INGEST_INLINE_MAX_BYTES=10485760
export INGEST_FILE_MAX_BYTES=52428800
export CHUNK_TARGET_CHARS=1000
export CHUNK_MAX_CHARS=1500
export CHUNK_OVERLAP_CHARS=100
```

### 3. Run database migrations

```bash
uv run alembic upgrade head
```

### 4. Start the API server

```bash
python -m ragent.api
```

### 5. Start the background worker

```bash
python -m ragent.worker
```

### 6. Verify

```bash
curl http://localhost:8000/livez
# {"status":"ok"}
```

### Development

**Linux / macOS**

```bash
make check        # format + lint + test
make test         # pytest with 92% coverage gate
make format       # ruff format
make lint         # ruff check --fix
```

**Windows** (run targets individually via `uv`)

```powershell
uv run ruff format .                                                          # format
uv run ruff check . --fix                                                     # lint
uv run pytest --cov=src/ragent --cov-branch --cov-fail-under=92              # test
```

---

## API Reference

All endpoints return RFC 9457 problem+json on errors. `X-User-Id` header is recorded for audit in Phase 1.

### Ingest (v2 — JSON only)

`POST /ingest` accepts a JSON body with discriminator `ingest_type ∈ {inline, file}`.
Supported MIME types (`content_type`): `text/plain`, `text/markdown`, `text/html`. CSV is no longer accepted.

#### `POST /ingest` — `ingest_type=inline` (content in body)

Cap: `INGEST_INLINE_MAX_BYTES` (default 10 MB) on the UTF-8 byte length of `content`.

```bash
curl -X POST http://localhost:8000/ingest \
  -H "X-User-Id: user-123" -H "Content-Type: application/json" \
  -d '{
    "ingest_type":      "inline",
    "content_type":     "text/markdown",
    "content":          "# Q3 OKRs\n\n```python\npool = create_pool()\n```",
    "source_id":        "DOC-123",
    "source_app":       "confluence",
    "source_title":     "Q3 OKR Planning",
    "source_workspace": "engineering",
    "source_url":       "https://wiki.example/q3-okr"
  }'
```

#### `POST /ingest` — `ingest_type=file` (object in MinIO)

The server reads from `(minio_site, object_key)` directly — no copy, no post-READY delete (we don't own the object). `minio_site` must be a name configured in `MINIO_SITES`. Cap: `INGEST_FILE_MAX_BYTES` (default 50 MB) verified at API time via HEAD-probe.

```bash
curl -X POST http://localhost:8000/ingest \
  -H "X-User-Id: user-123" -H "Content-Type: application/json" \
  -d '{
    "ingest_type":      "file",
    "content_type":     "text/html",
    "minio_site":       "tenant-eu-1",
    "object_key":       "reports/2025.html",
    "source_id":        "DOC-456",
    "source_app":       "s3-importer",
    "source_title":     "Annual Report 2025",
    "source_url":       "https://example.com/reports/2025"
  }'
```

```json
// 202 Accepted (both forms)
{ "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ" }
```

The returned `document_id` is the same identifier used by `GET /ingest/{document_id}` and `DELETE /ingest/{document_id}`.

**Errors (RFC 9457 problem+json):**
- `415 INGEST_MIME_UNSUPPORTED` — `content_type` not in allow-list.
- `413 INGEST_FILE_TOO_LARGE` — inline content or file size exceeds the cap.
- `422 INGEST_VALIDATION` — discriminator/required-field shape errors.
- `422 INGEST_MINIO_SITE_UNKNOWN` — `minio_site` not in registry.
- `422 INGEST_OBJECT_NOT_FOUND` — `(minio_site, object_key)` HEAD-probe miss.

#### `GET /ingest/{document_id}` — Get document status

```bash
curl http://localhost:8000/ingest/01J9ABCDEFGHJKMNPQRSTVWXYZ
```

```json
// 200 OK
{
  "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
  "status": "READY",
  "attempt": 1,
  "updated_at": "2026-05-05T10:00:00.000000",
  "ingest_type": "inline",
  "minio_site": null,
  "source_id": "DOC-123",
  "source_app": "confluence",
  "source_title": "Q3 OKR Planning",
  "source_url": "https://wiki.example/q3-okr"
}
```

Status values: `UPLOADED → PENDING → READY | FAILED`; `DELETING` during delete.
For `ingest_type=file` rows, `minio_site` is the registered site name (e.g. `tenant-eu-1`); for `ingest_type=inline` it is `null` and bytes were staged to `__default__`.

#### `GET /ingest` — List documents (cursor-paginated)

```bash
curl "http://localhost:8000/ingest?limit=20&after=01J9..."
```

```json
// 200 OK
{
  "items": [
    {
      "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
      "status": "READY",
      "source_id": "DOC-123",
      "source_app": "confluence",
      "source_title": "Q3 OKR Planning",
      "updated_at": "2026-05-05T10:00:00.000000"
    }
  ],
  "next_cursor": "01J9..."
}
```

#### `DELETE /ingest/{document_id}` — Delete a document

Cascade-deletes chunks from ES and all plugin stores.

```bash
curl -X DELETE http://localhost:8000/ingest/01J9ABCDEFGHJKMNPQRSTVWXYZ
# 204 No Content
```

---

### Chat

Request schema is shared by both endpoints. Only `messages` is required.

```json
{
  "messages": [
    { "role": "user", "content": "What are our Q3 OKRs?" }
  ],
  "provider": "openai",
  "model": "gptoss-120b",
  "temperature": 0.7,
  "max_tokens": 4096,
  "source_app": "confluence",
  "source_workspace": "engineering"
}
```

`source_app` and `source_workspace` are optional retrieval filters (AND when both supplied; omit to retrieve across all documents).

#### `POST /chat` — Non-streaming chat

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{
    "messages": [{"role": "user", "content": "What are our Q3 OKRs?"}],
    "source_app": "confluence"
  }'
```

```json
// 200 OK
{
  "content": "Based on the Q3 OKR Planning document, your objectives are...",
  "usage": { "promptTokens": 512, "completionTokens": 128, "totalTokens": 640 },
  "model": "gptoss-120b",
  "provider": "openai",
  "sources": [
    {
      "document_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ",
      "source_app": "confluence",
      "source_id": "DOC-123",
      "type": "knowledge",
      "source_title": "Q3 OKR Planning",
      "excerpt": "Key results for Q3 include..."
    }
  ]
}
```

#### `POST /chat/stream` — Streaming chat (SSE)

```bash
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{"messages": [{"role": "user", "content": "Summarise our roadmap"}]}' \
  --no-buffer
```

```
data: {"type": "delta", "content": "Based"}
data: {"type": "delta", "content": " on"}
data: {"type": "delta", "content": " the documents..."}
data: {"type": "done", "content": "Based on the documents...", "model": "gptoss-120b", "provider": "openai", "sources": [...]}
```

Error event: `{"type": "error", "error_code": "LLM_ERROR", "message": "..."}`

---

### Retrieve

#### `POST /retrieve` — Retrieve chunks without LLM

Runs the full retrieval pipeline (embed → kNN + BM25 → RRF join → source hydration) and returns ranked chunks directly, without invoking the LLM. Useful for debugging retrieval quality or building custom UIs.

By default returns **all ranked chunks** — a single document can appear multiple times if several of its chunks scored highly. Set `"dedupe": true` to keep only the best-scoring chunk per `document_id`.

```bash
curl -X POST http://localhost:8000/retrieve \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{
    "query": "What are our Q3 OKRs?",
    "source_app": "confluence",
    "source_workspace": "engineering",
    "dedupe": true
  }'
```

```json
// 200 OK — dedupe=false (default): same document_id can repeat
{
  "chunks": [
    {
      "document_id": "01J9AAA",
      "source_app": "confluence",
      "source_id": "DOC-123",
      "type": "knowledge",
      "source_title": "Q3 OKR Planning",
      "excerpt": "Key results for Q3 include..."
    },
    {
      "document_id": "01J9AAA",
      "source_app": "confluence",
      "source_id": "DOC-123",
      "type": "knowledge",
      "source_title": "Q3 OKR Planning",
      "excerpt": "Another chunk from the same document..."
    }
  ]
}
```

```json
// 200 OK — dedupe=true: one entry per document_id (highest-scored chunk wins)
{
  "chunks": [
    {
      "document_id": "01J9AAA",
      "source_app": "confluence",
      "source_id": "DOC-123",
      "type": "knowledge",
      "source_title": "Q3 OKR Planning",
      "excerpt": "Key results for Q3 include..."
    }
  ]
}
```

**Request fields:**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | `string` | Yes | — | Retrieval query text |
| `source_app` | `string` | No | — | ES filter; omit for unrestricted retrieval |
| `source_workspace` | `string` | No | — | ES filter; ANDed with `source_app` when both supplied |
| `dedupe` | `bool` | No | `false` | When `true`, keeps only the highest-scored chunk per `document_id` |

**How `excerpt` works:**

Each chunk stored in ES is the raw text segment produced by the indexing pipeline's splitter. The `excerpt` field in the response is that chunk's text, truncated to `EXCERPT_MAX_CHARS` characters (default `512`, configurable via env var) by `SourceHydrator` before it reaches the router. Truncation is a hard character cut — no semantic boundary is preserved. The same truncation applies to `sources[].excerpt` in `/chat` and `/chat/stream` responses.

---

### Observability

| Endpoint | Description |
|---|---|
| `GET /livez` | Liveness probe — always 200 if process is up |
| `GET /readyz` | Readiness probe — checks all dependencies (DB, ES, Redis, MinIO) |
| `GET /metrics` | Prometheus metrics (text/plain) |

```bash
curl http://localhost:8000/readyz
# {"status":"ok"}

curl http://localhost:8000/metrics
# # HELP reconciler_tick_total ...
```

### MCP (Phase 2)

`POST /mcp/tools/rag` — Returns `501 MCP_NOT_IMPLEMENTED` in Phase 1.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Clients                                │
│              (Browser / Confluence / Slack / …)                 │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                       FastAPI Router                            │
│   POST /ingest   GET /ingest   DELETE /ingest   POST /chat      │
│   POST /chat/stream   POST /retrieve                            │
│   GET /livez   GET /readyz   GET /metrics                       │
└──────┬───────────────────────────────────────┬──────────────────┘
       │ kiq task                              │ sync pipeline
       ▼                                       ▼
┌─────────────────┐              ┌─────────────────────────────────┐
│  Redis Sentinel │              │         Chat Pipeline           │
│  (task broker)  │              │  QueryEmbedder                  │
└────────┬────────┘              │  ├── ESVectorRetriever (kNN)    │
         │                      │  └── ESBM25Retriever (BM25)     │
         ▼                      │  DocumentJoiner (RRF)           │
┌─────────────────┐             │  SourceHydrator (JOIN docs)     │
│  TaskIQ Worker  │             │  LLMClient.chat / .stream       │
│                 │             └──────────────┬──────────────────┘
│  Ingest Pipeline│                            │
│  (v2)           │             ┌──────────────▼──────────────────┐
│  ┌────────────┐ │             │        Elasticsearch 9.x        │
│  │_TextLoader │ │             │  chunks_v1 (kNN + BM25 + raw)   │
│  │MimeRouter  │─┼────────────►│                                 │
│  │ ├ Plain    │ │             └─────────────────────────────────┘
│  │ ├ Markdown │ │
│  │ └ HTML AST │ │             ┌─────────────────────────────────┐
│  │BudgetChunker│─┼────────────►│           MariaDB 10.6          │
│  │Embedder    │ │             │  documents (metadata only)       │
│  │DocWriter   │ │             │  (v1 `chunks` table dropped)     │
│  └────────────┘ │             └─────────────────────────────────┘
│                 │
│  fan_out()      │             ┌─────────────────────────────────┐
└─────────────────┘────────────►│        Plugin Registry          │
         │                     │  VectorExtractor (required)     │
         ▼                     │  StubGraphExtractor (optional)  │
┌─────────────────┐             └─────────────────────────────────┘
│  MinIO (sites)  │
│  __default__:   │             ┌─────────────────────────────────┐
│   inline staging│             │       Third-Party APIs          │
│   deleted READY │             │  EmbeddingClient  (bge-m3)      │
│  caller sites:  │             │  LLMClient        (gptoss-120b) │
│   read-only,    │             │  RerankClient     (P2)          │
│   no delete     │             └─────────────────────────────────┘
└─────────────────┘

Observability: OpenTelemetry → Grafana / Prometheus
Reconciler:    CronJob → re-dispatches stale PENDING / UPLOADED rows
```

### Key Design Decisions

- **JSON-only ingest with discriminator** — `POST /ingest` takes `{ingest_type: "inline"|"file", ...}`; multipart is gone. Inline content is staged to the `__default__` MinIO site and deleted after `READY`. File-type ingests read directly from the caller-owned `(minio_site, object_key)` and are never deleted.
- **MIME-aware AST splitters** — `text/markdown` uses mistletoe (fenced code never split, atoms carry the original markdown); `text/html` uses selectolax (drops `<script>/<nav>/<aside>/<footer>/<header>` boilerplate, preserves `<pre>`/`<table>` atomically); `text/plain` uses Haystack's stock `DocumentSplitter`. CSV is no longer accepted.
- **Mime-agnostic budget chunker** — single 1000/1500/100 (target/max/overlap) profile across all MIMEs; the v1 EN/CJK/CSV branches and the `langdetect`/`nltk` deps are gone.
- **Embed clean, return raw** — each ES chunk carries both `content` (normalized text used for BM25 scoring + bge-m3 embedding) and `raw_content` (original byte slice with markdown fences / HTML tags intact). Chat citations and LLM context use `raw_content`; retrieval scoring stays on `content`.
- **Chunks live only in ES** — the v1 MariaDB `chunks` table was dropped in v2. MariaDB stores document metadata only.
- **Two-transaction locking** — TX-A acquires row lock and writes `PENDING`, then commits (releases lock) before the pipeline body runs. No DB transaction is held during external calls (embedder, ES, plugins).
- **Worker heartbeat** — updates `documents.updated_at` every 30 s so the Reconciler distinguishes live workers from crashed ones.
- **Supersede model** — re-POSTing with the same `(source_id, source_app)` creates a new document; on `READY`, a supersede task cascade-deletes older versions, giving zero-downtime replacement.
- **Hybrid retrieval** — kNN vector search + BM25 full-text joined with Reciprocal Rank Fusion (configurable via `CHAT_JOIN_MODE`).
- **Per-step structured logs** — every pipeline component emits `ingest.step.{started,ok,failed}` on `ragent.ingest` with `document_id`/`mime_type` bound; the worker emits a terminal `ingest.ready` / `ingest.failed`.

---

## Docs

| File | Purpose |
|---|---|
| `docs/00_rule.md` | Development standards and mandatory workflow |
| `docs/00_spec.md` | Full technical specification |
| `docs/00_plan.md` | TDD implementation checklist |
| `docs/00_agent_team.md` | Agent team and workflow |
| `docs/00_journal.md` | Team reflection and blameless guidelines |
