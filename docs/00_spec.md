# 00_spec.md — Distributed RAG Agent System Specification (WHAT)

> Source: `docs/draft.md` · Authored: 2026-05-03 · Revised: 2026-05-04
> Standard: `docs/00_rule.md` §Specification Standards (WHAT, not HOW)
> Revision driver: team review `docs/team/2026_05_04_phase1_review.md`

---

## 1. Mission & Objective

Provide an enterprise internal knowledge retrieval backend that ingests private documents under organizational ACL (resolved through OpenFGA), serves grounded answers via a streaming chat API, and exposes the same retrieval capability through an MCP tool. The system delivers **resilient, recoverable, permission-aware** RAG over hybrid (vector + BM25) retrieval, with a **pluggable extractor architecture** that admits graph reasoning in Phase 3 without modifying the main pipeline.

**Non-Goals (System-wide):**
- No local model hosting (all inference via third-party APIs documented in `00_rule.md`).
- No frontend (REST / SSE / MCP only).
- No public/anonymous access (JWT subject + OpenFGA mandatory).

---

## 2. Domain Boundary

| Domain Topic | Responsibilities | Out-of-Scope |
|---|---|---|
| **Ingest Lifecycle** | `POST /ingest` → MinIO put → `documents` row (`UPLOADED`) → TaskIQ dispatch. State machine `UPLOADED → PENDING → READY \| FAILED`. Pessimistic lock on every status mutation. | Document authoring, OCR tuning, model fine-tuning. |
| **Indexing Pipeline** | Worker: pickup (lock, status=PENDING, attempt+=1) → Convert → Clean → LanguageRouter → CN/EN Splitter → Embedder → write `chunks` (Repository) → `PluginRegistry.fan_out(document_id)`. | Re-ranking, query understanding. |
| **Pluggable Extractors** | `ExtractorPlugin` Protocol v1 (frozen P1). `PluginRegistry` (explicit register, per-plugin TaskIQ queue). P1 plugins: `VectorExtractor` (required), `StubGraphExtractor` (optional, no-op). | Real graph extraction (P3). |
| **Retrieval & Chat** | JWT verify → OpenFGA `list_resource` → ES `terms` filter on `document_id` → parallel ES vector + BM25 → `DocumentJoiner` (RRF) → OpenFGA `check` per result → LLM stream (SSE). | Intent routing (P2), Rerank wiring (P2 — client built P1), graph retrieval (P3). |
| **Resilience** | Redis broker + Redis rate-limiter (separate instances). Reconciler (5-min cron, idempotent re-dispatch by `(document_id, attempt)`). Circuit-breaker + retry on every 3rd-party client. | Multi-region replication, DR drills. |
| **Auth & Permission** | JWT validation (subject claim = user_id). OpenFGA dual-layer: `list_resource` (pre-filter, per-request cached) + `check` (post-filter). TokenManager: J1→J2 exchange + refresh `expiresAt − 5 min`. | User management UI, SSO provisioning, HR resolution (P2). |
| **Observability** | Haystack auto-trace + FastAPI OTEL middleware → Tempo + Prometheus. Structured logs for state-machine transitions and circuit-breaker events. | Custom dashboards (Phase 2). |
| **Code Standards** | Layered: Router (HTTP only) → Service (orchestration) → Repository (CRUD only). Methods ≤ 30 LOC. ≤ 2-level nesting. Utilities in `utility/`. | Cross-layer leaks (e.g., DB calls in routers). |

---

## 3. Business Process (High-level Flowcharts)

### 3.1 Ingest (async, full lifecycle)

```
[Client] --POST /ingest (JWT, multipart)--> [Router]
   [Router] -> IngestService.create_ingest(file, user_id)
        ├─ MinIOClient.put_object(file)
        ├─ DocumentRepository.create(
        │     id=uuidv7-base32(26), owner_user_id=user_id,
        │     storage_uri="minio://...", status=UPLOADED, attempt=0)
        └─ TaskIQ.kiq("ingest.pipeline", document_id)
   [Router] -> 202 { "task_id": document_id }
                                                 │
                                                 ▼
[Worker @ ingest.pipeline]
   ├─ DocumentRepository.acquire(document_id)         # SELECT … FOR UPDATE
   │     status=PENDING, attempt+=1
   ├─ Haystack Ingest (Convert→Clean→Lang→Split→Embed)
   ├─ ChunkRepository.bulk_insert(chunks)
   ├─ PluginRegistry.fan_out(document_id)             # per-plugin queue
   ├─ all required ok → status=READY
   ├─ any required error → status=PENDING (Reconciler retries)
   └─ attempt > 5 → status=FAILED + structured-log alert

[Reconciler @ cron 5m]
   SELECT … WHERE status=PENDING AND age > 5m FOR UPDATE SKIP LOCKED
   ├─ attempt ≤ 5 → re-kiq (idempotent by (document_id, attempt))
   └─ attempt > 5 → status=FAILED + alert
```

### 3.2 Chat (sync, SSE)

```
[Client] --POST /chat (JWT, query)--> [Router]
   [Router] -> verify_jwt → user_id
   [Router] -> ChatService.stream(user_id, query)
        ├─ OpenFGAClient.list_resource(user_id, "can_view", "kms_page")
        │     → cached for THIS request only
        ├─ ES query with terms filter document_id ∈ whitelist
        ├─ Haystack Chat (parallel ES vector + BM25 → RRF Joiner)
        ├─ for each candidate chunk:
        │     OpenFGAClient.check(user_id, "can_view", "kms_page", document_id)
        │     drop on mismatch + audit log
        └─ LLMClient.stream(messages) → SSE deltas + final done
```

---

## 4. Business Scenario (Mermaid)

### 4.1 Plugin Fan-out with Required/Optional contract

```mermaid
sequenceDiagram
  participant W as Worker
  participant R as PluginRegistry
  participant V as VectorExtractor (required)
  participant G as StubGraphExtractor (optional)
  participant DR as DocumentRepository
  W->>R: fan_out(document_id)
  par
    R->>V: extract.vector queue
    V-->>R: Result(ok=True)
  and
    R->>G: extract.graph queue
    G-->>R: Result(ok=True, no-op)
  end
  R-->>W: {vector: ok, graph_stub: ok}
  alt all required ok
    W->>DR: update_status(READY)
  else any required error
    W->>DR: update_status(PENDING)
  end
```

### 4.2 Reconciler

```mermaid
sequenceDiagram
  participant R as Reconciler (cron 5m)
  participant DB as MariaDB
  participant Q as TaskIQ
  loop every 5 minutes
    R->>DB: SELECT … FOR UPDATE SKIP LOCKED
    alt attempt ≤ 5
      R->>Q: kiq ingest.pipeline (idempotent by (document_id, attempt))
    else attempt > 5
      R->>DB: status=FAILED
      R-->>R: structured log "event=ingest.failed"
    end
  end
```

### 4.3 OpenFGA Dual-Layer Permission

```mermaid
flowchart LR
  A[/chat request/] --> B{verify_jwt}
  B -- invalid --> X[401]
  B -- ok --> C[OpenFGA list_resource → whitelist]
  C --> D[ES query with terms filter document_id ∈ whitelist]
  D --> E[RRF Joiner]
  E --> F{OpenFGA check per chunk}
  F -- mismatch --> G[Drop + audit log]
  F -- allowed --> H[LLM stream]
```

### 4.4 TokenManager Refresh

```mermaid
sequenceDiagram
  participant C as AI Client
  participant T as TokenManager
  participant A as AuthService
  C->>T: get_token()
  alt cached and now < expiresAt - 5min
    T-->>C: cached J2
  else
    T->>A: POST /auth/api/accesstoken {key=J1}
    A-->>T: {token: J2, expiresAt}
    T->>T: cache(J2, expiresAt)
    T-->>C: J2
  end
```

---

## 5. Scenario Testing (Given-When-Then) — Phase 1

### Domain: Ingest Lifecycle

- **S1 happy path** — Given a JWT-authenticated user, When they POST a 1 MB PDF to `/ingest`, Then the response is 202 with `task_id` (26-char base32), MinIO contains the original, `documents.status=UPLOADED`, and within 60 s status transitions to `READY` with chunks in Elasticsearch.
- **S2 reconciler recovery** — Given a worker crashes after `status=PENDING` but before fan-out completes, When 5 min elapse, Then Reconciler re-kiqs `(document_id, attempt+1)` and the task completes idempotently (no duplicate ES docs).
- **S3 failed after retries** — Given a required plugin fails 5 times, When the 6th attempt would fire, Then `status=FAILED`, structured-log line `event=ingest.failed document_id=… attempt=6` is emitted.
- **S10 state-machine negative paths** (per `00_journal.md` rule) — Given any document, When transitioning UPLOADED→FAILED, READY→PENDING, or FAILED→READY, Then `update_status()` raises `IllegalStateTransition`.

### Domain: Pluggable Pipeline

- **S4 protocol contract** — Given a class declaring conformance to `ExtractorPlugin`, When inspected, Then it must expose `name / required / queue / extract / delete / health`.
- **S5 stub graph no-op** — Given `StubGraphExtractor` registered, When fan-out fires, Then `extract` returns no side effects and overall ingest still reaches READY.
- **S11 registry invariants** — Given two plugins registered with the same `name`, When the second registers, Then `register()` raises `DuplicatePluginError`. And given a plugin whose required call errors, `fan_out` returns a result map where `all_required_ok(results) is False`.

### Domain: Chat / Retrieval

- **S6 hybrid retrieval** — Given indexed corpus, When JWT user POSTs query to `/chat`, Then SSE stream emits ≥ 1 `delta` followed by exactly one `done` whose `sources` are a subset of OpenFGA `list_resource` result.
- **S7 OpenFGA dual-filter** — Given user A whose `list_resource` excludes doc_X, When ES is forced to return doc_X via direct injection, Then `check` rejects it post-filter and the chunk does not appear in `sources`; an audit log is emitted.

### Domain: Third-Party API & Auth

- **S8 MCP schema-only** — Given Phase 1 scope, When client calls `POST /mcp/tools/rag`, Then OpenAPI schema is published but handler returns `501 Not Implemented`.
- **S9 token refresh boundary** — Given a J2 token whose `expiresAt` is `now + 4 min 59 s`, When any client requests a token via `TokenManager`, Then `TokenManager` re-exchanges before returning (≥ 5 min margin enforced; uses fake clock in tests).

---

## 6. System Interface

### 6.1 REST / SSE

| Method | Path | Auth | Request | Response |
|---|---|---|---|---|
| POST | `/ingest` | JWT | `multipart/form-data: file` | 202 `{ "task_id": "<26-char-base32>" }` |
| GET  | `/ingest/{document_id}` | JWT | — | 200 `{ "status": "UPLOADED\|PENDING\|READY\|FAILED", "attempt": int, "updated_at": "<ISO 8601 Z>" }` |
| POST | `/chat` | JWT | `{ "query": str }` | `text/event-stream` (`delta`*, `done`) |
| POST | `/mcp/tools/rag` | JWT | `{ "query": str }` | **501 Not Implemented** (P1); P2 → `{ answer, sources[] }` |

**SSE event payloads:**

```jsonc
{ "event": "delta", "data": { "text": "..." } }
{ "event": "done",  "data": { "answer": "...", "sources": [{ "id", "title", "url" }] } }
```

### 6.2 Plugin Protocol v1 (frozen)

```python
@runtime_checkable
class ExtractorPlugin(Protocol):
    name: str
    required: bool
    queue: str
    def extract(self, document_id: str) -> None: ...
    def delete(self, document_id: str) -> None: ...
    def health(self) -> bool: ...
```

### 6.3 PluginRegistry contract

```python
class PluginRegistry:
    def register(self, plugin: ExtractorPlugin) -> None: ...   # raises DuplicatePluginError
    def fan_out(self, document_id: str) -> dict[str, Result]:  # name -> Result(ok, error?)
    def all_required_ok(self, results: dict[str, Result]) -> bool: ...
```

### 6.4 Third-Party API integration (per `00_rule.md` §Third-Party API)

| Client | Endpoint | Auth | Used by | Phase |
|---|---|---|---|---|
| `EmbeddingClient` | `EMBEDDING_API_URL/text_embedding` | J2 (TokenManager) | Indexing + QueryEmbedder | P1 |
| `LLMClient` | `LLM_API_URL/gpt_oss_120b/v1/chat/completions` | J2 (TokenManager) | Chat stream | P1 |
| `RerankClient` | `REREANK_API_URL/` | J2 (TokenManager) | Chat (wired in P2) | P1 unit / P2 wiring |
| `OpenFGAClient` | `OPENFGA_API_URL/{list_resource,check}` | `gam-key` header | Auth dual-filter | P1 |
| `TokenManager` | `AI_API_AUTH_URL/auth/api/accesstoken` | J1 → J2 | Embedding/LLM/Rerank | P1 |
| `HRClient` | `HR_API_URL/v3/employees` | `Authorization` header | Owner resolution | **P2** |

All 3rd-party calls: timeout/retry/backoff per `00_rule.md`; circuit-breaker on the client; structured logs.

---

## 7. Data Structure

### 7.1 MariaDB (no physical FK; ORM-only relations)

```sql
CREATE TABLE documents (
  document_id    CHAR(26)    PRIMARY KEY,
  owner_user_id  VARCHAR(64) NOT NULL,
  storage_uri    VARCHAR(512) NOT NULL,
  status         ENUM('UPLOADED','PENDING','READY','FAILED') NOT NULL,
  attempt        INT          NOT NULL DEFAULT 0,
  created_at     DATETIME(6)  NOT NULL,
  updated_at     DATETIME(6)  NOT NULL,
  INDEX idx_status_updated (status, updated_at)
);

CREATE TABLE chunks (
  chunk_id    CHAR(26)    PRIMARY KEY,
  document_id CHAR(26)    NOT NULL,
  ord         INT         NOT NULL,
  text        MEDIUMTEXT  NOT NULL,
  lang        VARCHAR(8)  NOT NULL,
  INDEX idx_document (document_id)
);
```

### 7.2 Elasticsearch index `chunks_v1`

```jsonc
{
  "settings": { "index": { "number_of_shards": 1, "number_of_replicas": 1 } },
  "mappings": {
    "properties": {
      "chunk_id":    { "type": "keyword" },
      "document_id": { "type": "keyword" },
      "lang":        { "type": "keyword" },
      "text":        { "type": "text", "analyzer": "standard" },
      "embedding":   { "type": "dense_vector", "dims": 1024, "index": true, "similarity": "cosine" }
    }
  }
}
```

> ACL is no longer stored on chunks. OpenFGA `list_resource` supplies the pre-filter; ES filters on `document_id ∈ whitelist`.

### 7.3 ID Generation Utility

```python
# src/ragent/utility/id_gen.py  (≤ 30 LOC)
def new_id() -> str:
    """UUIDv7 → 16 bytes → Crockford Base32 → 26 chars."""
```

### 7.4 DateTime Utility

```python
# src/ragent/utility/datetime.py  (≤ 30 LOC)
def utcnow() -> datetime: ...        # tz=UTC always
def to_iso(dt: datetime) -> str: ... # "...Z"
def from_db(naive: datetime) -> datetime: ...  # .replace(tzinfo=UTC)
```
