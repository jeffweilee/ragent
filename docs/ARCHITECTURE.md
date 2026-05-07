# Architecture

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

## Key Design Decisions

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
