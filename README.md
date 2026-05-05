# ragent

Enterprise internal knowledge retrieval backend — streaming RAG answers grounded in private documents.

**Phase 1:** Ingest CRUD + Indexing Pipeline + Chat (non-streaming & SSE). Auth disabled (`X-User-Id` header trusted, audit only).

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
export DATABASE_URL="mysql+pymysql://user:pass@localhost:3306/ragent"
export REDIS_SENTINEL_HOSTS="localhost:26379"
export REDIS_SENTINEL_MASTER="mymaster"
export ES_HOST="http://localhost:9200"
export MINIO_ENDPOINT="localhost:9000"
export MINIO_ACCESS_KEY="minioadmin"
export MINIO_SECRET_KEY="minioadmin"

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
export INGEST_MAX_FILE_SIZE_BYTES=52428800
```

### 3. Run database migrations

```bash
uv run alembic upgrade head
```

### 4. Start the API server

```bash
uv run uvicorn ragent.api:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Start the background worker

```bash
uv run taskiq worker ragent.broker:broker ragent.tasks
```

### 6. Verify

```bash
curl http://localhost:8000/livez
# {"status":"ok"}
```

### Development

```bash
make check        # format + lint + test
make test         # pytest with 92% coverage gate
make format       # ruff format
make lint         # ruff check --fix
```

---

## API Reference

All endpoints return RFC 9457 problem+json on errors. `X-User-Id` header is recorded for audit in Phase 1.

### Ingest

#### `POST /ingest` — Upload a document

Accepts `multipart/form-data`. Supported MIME types: `text/plain`, `text/markdown`, `text/csv`.

```bash
curl -X POST http://localhost:8000/ingest \
  -H "X-User-Id: user-123" \
  -F "file=@report.txt" \
  -F "source_id=DOC-123" \
  -F "source_app=confluence" \
  -F "source_title=Q3 OKR Planning" \
  -F "source_workspace=engineering"
```

```json
// 202 Accepted
{ "task_id": "01J9ABCDEFGHJKMNPQRSTVWXYZ" }
```

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
  "updated_at": "2026-05-05T10:00:00.000000"
}
```

Status values: `UPLOADED → PENDING → READY | FAILED`; `DELETING` during delete.

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
      "title": "Q3 OKR Planning",
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
│   POST /chat/stream   GET /livez   GET /readyz   GET /metrics   │
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
│  ┌────────────┐ │             ┌──────────────▼──────────────────┐
│  │FileTypeRouter│ │            │        Elasticsearch 9.x        │
│  │Converter   │ │             │  chunks_v1 (kNN + BM25 index)   │
│  │RowMerger   │─┼────────────►│                                 │
│  │DocCleaner  │ │             └─────────────────────────────────┘
│  │LangRouter  │ │
│  │Splitter    │ │             ┌─────────────────────────────────┐
│  │Embedder    │─┼────────────►│           MariaDB 10.6          │
│  │ChunkRepo   │ │             │  documents  chunks  (metadata)  │
│  │PluginReg.  │ │             └─────────────────────────────────┘
│  └────────────┘ │
│                 │             ┌─────────────────────────────────┐
│  fan_out()      │────────────►│        Plugin Registry          │
└─────────────────┘             │  VectorExtractor (required)     │
         │                     │  StubGraphExtractor (optional)  │
         ▼                     └─────────────────────────────────┘
┌─────────────────┐
│   MinIO (staging│             ┌─────────────────────────────────┐
│   upload only;  │             │       Third-Party APIs          │
│   deleted after │             │  EmbeddingClient  (bge-m3)      │
│   READY/FAILED) │             │  LLMClient        (gptoss-120b) │
└─────────────────┘             │  RerankClient     (P2)          │
                                └─────────────────────────────────┘

Observability: OpenTelemetry → Grafana / Prometheus
Reconciler:    CronJob → re-dispatches stale PENDING / UPLOADED rows
```

### Key Design Decisions

- **MinIO is transient staging only** — files are deleted after pipeline reaches `READY` or `FAILED`; only chunks (ES) and metadata (MariaDB) persist.
- **Two-transaction locking** — TX-A acquires row lock and writes `PENDING`, then commits (releases lock) before the pipeline body runs. No DB transaction is held during external calls (embedder, ES, plugins).
- **Worker heartbeat** — updates `documents.updated_at` every 30 s so the Reconciler distinguishes live workers from crashed ones.
- **Supersede model** — re-POSTing with the same `(source_id, source_app)` creates a new document; on `READY`, a supersede task cascade-deletes older versions, giving zero-downtime replacement.
- **Hybrid retrieval** — kNN vector search + BM25 full-text joined with Reciprocal Rank Fusion (configurable via `CHAT_JOIN_MODE`).

---

## Docs

| File | Purpose |
|---|---|
| `docs/00_rule.md` | Development standards and mandatory workflow |
| `docs/00_spec.md` | Full technical specification |
| `docs/00_plan.md` | TDD implementation checklist |
| `docs/00_agent_team.md` | Agent team and workflow |
| `docs/00_journal.md` | Team reflection and blameless guidelines |
