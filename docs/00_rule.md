# Rule

- **Always** Check and Update following documents Before and After planning and delivery.
   - `docs/00_spec.md`: Specification Standards
   - `docs/00_plan.md`: Master TDD Implementation Checklist
   - `docs/00_journal.md` (Blameless Team Reflection)
- **(Mandatory)** Execute the full pre-commit sequence (**start Docker daemon** → format → lint → **full test suite including docker testcontainers integration tests** → security scan) before every commit. Do **not** skip `@pytest.mark.docker` tests; skipped docker tests are a blocking violation. Start the Docker daemon **in advance** so testcontainers (MariaDB, ES, Redis, MinIO) actually run. See `# Command` section.
- **Always** refer to `00_agent_team.md` and use "RAGENT Agent Team" workflow for planning, implementation, delivery.
- **Always** refer to "Context7" MCP for any library and framework standard spec and example.


## Document

### `docs/00_spec.md`: Specification Standards

| Section | Inclusion | Exclusion |
| :--- | :--- | :--- |
| **Mission & Objective** | System or module goals (**WHAT**) | Implementation methods, detailed steps (**HOW**) |
| **Domain Boundary** | System scope and inter-module relationships. Fields: Domain Topic, Responsibilities, Out-of-Scope | Functional requirement lists |
| **Business Process** | High-level business flows: Happy path, error handling. Use simple wireframe flowcharts (readable in 1s). | Granular logic branch flows, specific edge-case business scenarios |
| **Business Scenario** | Low-level business details. Use simple Mermaid flowcharts or sequence diagrams (readable in 1s). | Data models, interface definitions |
| **Scenario Testing** | Behavior-Driven Development (TDD/BDD). Fields: Domain, Scenario, Given, When, Then | Actual implementation code |
| **System Interface** | (Optional) API endpoints, Interface definitions, and samples | Internal implementation class or object naming details |
| **Data Structure** | (Optional) Database schemas/fields, Elasticsearch Index settings, and mappings | Internal implementation class or object or Data models or naming details |


### `docs/00_plan.md`: Master TDD Implementation Checklist

**Task column format (mandatory):** every task cell is rendered as a bulleted list (use `<br>•` separators inside the markdown cell). Each task **must** open with two one-line summary bullets, in this order:

1. `• **Achieve:** <one sentence — what the task accomplishes / why>`
2. `• **Deliver:** <one sentence — concrete artifact: file path, test path, env var, manifest, etc.>`

Any further specifics (constraints, env vars, edge cases, references) follow as additional `•` bullets in the same cell. Do not write the task as a single prose paragraph.

| Phase | Category | Task | Status | Owner |
| :--- | :--- | :--- | :---: | :--- |
| **Phase 1** | **Analysis** | • **Achieve:** Lock domain boundaries and mission objectives.<br>• **Deliver:** Updated sections in `docs/00_spec.md`. | [ ] | Architect |
| **Phase 1** | **Design** | • **Achieve:** Translate scenarios into executable behavior contracts.<br>• **Deliver:** Given-When-Then rows under `docs/00_spec.md` §Scenario Testing. | [ ] | QA / PM |
| **Phase 1** | **Red** | • **Achieve:** Pin behavior with failing tests before any production code.<br>• **Deliver:** Failing test files under `tests/{unit,integration,e2e}/`. | [ ] | QA / Dev |
| **Phase 1** | **Green** | • **Achieve:** Make the red tests pass with the minimum viable code.<br>• **Deliver:** Production modules under `src/ragent/` matching the test contract. | [ ] | Dev |
| **Phase 1** | **Refactor** | • **Achieve:** Tidy structure without changing behavior; enforce clean code, idempotency, performance.<br>• **Deliver:** Reviewed diff with green tests; review notes captured in commit/PR. | [ ] | Reviewer |
| **Phase 2** | **Stability** | • **Achieve:** Production-grade resilience and visibility.<br>• **Deliver:** HA verification report, Prometheus alert rules, Grafana dashboards. | [ ] | SRE |
| **Phase 2** | **Closure** | • **Achieve:** Close the loop on docs and lessons learned.<br>• **Deliver:** Updated `00_spec.md` / `00_plan.md` + new `00_journal.md` entries. | [ ] | Master |


### `docs/00_journal.md` (Blameless Team Reflection)

> **Goal:** Prevent recurrence through actionable, domain-specific guidelines rather than individual blame.

**Format:**
1. **Domain List (TOC)** at the top — a fixed, converged set of domains. New entries MUST be filed under an existing domain; do not invent new domains. Allowed domains: `Architecture`, `SRE`, `QA`, `Security`, `Spec`, `Process`.
2. **Per-Domain Table** — one section per domain, each containing a 5-column table. The `Topic` column is a short tag (1–3 words) that lets a reader scan the table and locate the relevant entry without reading every Description.

| Date | Topic | Description | Root Cause | Actionable Guideline |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-04 | Concurrency | Race condition during high-concurrency wallet updates. | Missing atomicity at the DB transaction level. | **[Rule]** All balance-related mutations must use Pessimistic Locking and be wrapped in an atomic decorator. |


---


## Standard

### Modules

- **High Cohesion, Low Coupling**
    - **Action**: All functions **must** be implemented as independent, pluggable modules to minimize inter-module dependencies.
    - **Constraint**: Nested `for` loops and `if-else` statements **must not** exceed 2 levels.
    - **Constraint**: A single method **must not** exceed 30 lines of code.
    - **Constraint**: Utility methods **must** be extracted to `utility.py` to keep the business logic in `service.py` clean.

- **Clear Layered Responsibilities**
    - **Presentation Layer (Router Layer)**:
        - **Responsibility**: Only handles HTTP request parsing, parameter validation, calling the service layer, and returning HTTP responses.
        - **Prohibition**: Inclusion of any business logic; direct database access.
    - **Service Layer (Service Layer)**:
        - **Responsibility**: Encapsulates and coordinates core business logic.
        - **Prohibition**: Handling HTTP-related operations; direct database CRUD (should go through the Repository Layer).
    - **Repository Layer (Repository Layer)**:
        - **Responsibility**: Dedicated to data persistence and retrieval (CRUD).
        - **Prohibition**: Inclusion of business logic.

---

### Database Practices

- **Rule: Mandatory Surrogate PK + Business Unique Key**
    - **Action**: Every new table **must** declare:
        1. A surrogate primary key `id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY` — used only for storage ordering and joins, never exposed in APIs or logs.
        2. The Crockford-Base32 business identifier (e.g. `document_id`) as `UNIQUE KEY` — this is the field the application, APIs, and logs reference.
        3. A `UNIQUE KEY` on the **business identity tuple** (e.g. `(source_id, source_app)`) so the database — not application code — refuses logical duplicates.
    - **Exception (eventual uniqueness)**: If the spec explicitly defines an **eventual-uniqueness** invariant for a tuple (e.g. supersede / revision-flip patterns where transient duplicates are expected mid-flight), the business-tuple `UNIQUE` is replaced by a non-unique composite index that supports the supersede query, **and** a schema comment in the migration must cite the spec section that authorizes the exception. Eventual uniqueness without a spec citation is forbidden.
    - **Rationale**: Surrogate PK keeps row width small and inserts append-only; business UNIQUE prevents duplicate-row bugs at the storage layer instead of relying on every code path to check first; the documented exception keeps the rule honest for designs that legitimately need transient duplicates.

- **Rule: No Physical Foreign Keys**
    - **Action**: Foreign key relationships are defined **only** within the application-level ORM models.
    - **Prohibition**: Do not use `FOREIGN KEY` constraints within the database Schema.
    - **Rationale**: Simplifies database migrations and improves bulk write performance.

- **Rule: Mandatory Indexing**
    - **Action**: All query fields used for `WHERE`, `JOIN`, and `ORDER BY` **must** have an established index.

- **Rule: Mandatory Connection Pool**
    - **Action**: Every DB-bound code path **must** acquire its connection from a pool (SQLAlchemy `engine` with default `QueuePool`, or an async pool such as `asyncmy` / `asyncpg` for async drivers). Acquire on entry to the unit of work, release on exit (`with engine.begin() as conn:` / `async with engine.connect() as conn:`).
    - **Prohibition**: Do **not** hold a single long-lived `Connection` (or `AsyncConnection`) at module / app-singleton scope and share it across requests, tasks, or threads.
    - **Rationale**: FastAPI is natively async; the event loop interleaves requests on a single worker, and a shared SQLAlchemy `Connection` is **not** safe for concurrent statements (raises "Packet sequence error" / "command out of sync" under load). Even sync routes get dispatched to a thread pool and hit the same hazard. A pool gives each unit of work an exclusive checkout and recycles the connection on return.
    - **Boundary**: Repositories accept either an `Engine` (and check out per call) or an injected per-request `Connection` from a `Depends`-driven session factory. Composition root holds the pool, never the connection.

---

### ID Generation Strategy: UUIDv7 + Base32

- **Rule**: Primary keys for all new tables **must** adopt this strategy.
- **Action**:
    1. Use a **UUIDv7** library to generate IDs.
    2. Encode the ID into a **26-character string** using **Crockford's Base32** before storage.
- **Rationale**: Sortable, decentralized, URL-safe, and human-readable.

---

### DateTime Handling: End-to-End UTC

- **Rule**: All timestamps **must** be stored, processed, and transmitted using the UTC standard.
- **Action**:
    1. During serialization, timestamps **must** be converted to an **ISO 8601** format string with a `+00:00` or `Z` suffix.
    2. When reading naive datetimes from the database, you **must** manually attach the UTC timezone (`.replace(tzinfo=UTC)`).
- **Prohibition**: Do not transmit or store any naive datetimes that lack timezone information.


---

### Logging: Identity Yes, Content No

- **Rule**: Business logs and API trace logs **must** carry identity fields needed for auditing and trace correlation, and **must not** carry sensitive content data.
- **Allowed (identity & metric)**: `service`, `request_id`, `trace_id`, `span_id`, `user_id`, `document_id` / `chunk_id` / `task_id` and other internal Crockford-Base32 IDs, `path`, `method`, `status_code`, `duration_ms`, `http.status_code`, counts (`top_k`, `result_count`, `batch_size`, `candidate_count`), sizes (`query_len`, `prompt_tokens`, `completion_tokens`), error metadata (`error_type`, `error_code`, `retry_attempt`).
- **Prohibited (content)**: raw user query text, prompt bodies, LLM completions, retrieved chunk text, document payloads, embedding vectors, request/response body bytes, `Authorization` / `Cookie` headers, tokens, secrets, passwords, and any field flagged sensitive by the data dictionary or PII catalogue.
- **Action**: When debugging context is needed, log a length, hash, or count instead of the value. Error logs follow the same rule; tracebacks **must not** be enriched with request body content.
- **Enforcement**: A denylist processor in `ragent.bootstrap.logging_config` drops the keys `query`, `prompt`, `messages`, `completion`, `chunks`, `embedding`, `documents`, `body`, `authorization`, `cookie`, `password`, `token`, `secret` from every emitted record as a safety net. The allow-list above is the policy contract; the denylist is the runtime guardrail.
- **Format**: Timestamps are ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SS.sssZ`), aligned with the DateTime rule. Output is JSON to stdout in production (`LOG_FORMAT=json`); developer-friendly key=value rendering is available via `LOG_FORMAT=console`.
- **Naming convention** (one canonical string per work unit; the **same string** is used for both the OTEL span name and the structlog event name so log↔trace correlation is trivial):
  - `api.request` / `api.error` — middleware-emitted, exactly one per HTTP request.
  - `<router>.request` — router entry span (e.g. `chat.request`, `retrieve.request`).
  - `<router>.<stage>` — sub-step inside a router (e.g. `chat.retrieval`, `chat.build_messages`, `chat.llm`, `retrieve.pipeline`, `retrieve.dedupe`).
  - `<peer>.<verb>` — outbound HTTP client call (e.g. `llm.chat`, `llm.stream`, `embedding.embed`, `rerank.score`); error variant `<peer>.error`.
  - `<domain>.<event>` — business state transitions (e.g. `ingest.failed`, `reconciler.tick`, `reconciler.redispatch`, `es.index_created`, `schema.drift`).
  - All names are lowercase, dot-separated, ≤ 4 segments. New names must follow the same shape; reuse an existing prefix before inventing a new one.

---

### OpenTelemetry: Initialize Once, Re-init After Fork

- **Rule**: The global `TracerProvider` is set **exactly once per OS process**. Do not replace it at runtime; do not call `set_tracer_provider` from request paths, hot-reload paths, or library code.
  - **Rationale**: `ProxyTracer` (returned by module-level `_tracer = trace.get_tracer(__name__)`) caches its real delegate on first span call. Replacing the global provider afterwards leaks spans to the dead provider — silent data loss in production, flaky tests in CI.

- **Rule: Initialize before any tracer use.** `setup_tracing(service_name)` **must** be the first call in `create_app()`, worker entrypoint, and reconciler entrypoint — before any router/client module is imported into the request path.
  - **Action**: keep `setup_tracing()` idempotent — if a real `TracerProvider` is already installed, return without re-setting. This protects against in-process module reloads.

- **Rule: Re-initialize after `fork()`.** When deploying under gunicorn `--preload` (or any `os.fork()` model), `BatchSpanProcessor`'s background flush thread does **not** survive the fork; spans queue forever and never export.
  - **Action**: in `gunicorn.conf.py`, hook `post_fork` to call `shutdown_tracing()` then `setup_tracing(service_name)` inside each worker. Also hook `worker_exit` to `shutdown_tracing()` so the queue flushes before the worker dies.
  - **`shutdown_tracing()` contract**: call `provider.shutdown()` (flushes BatchSpanProcessor, closes OTLP connection), then reset `opentelemetry.trace._TRACER_PROVIDER_SET_ONCE._done = False` and `_TRACER_PROVIDER = None` so the next `setup_tracing()` succeeds.

- **Rule: Use `BatchSpanProcessor` in production, never `SimpleSpanProcessor`.** Simple exports synchronously on the request thread; batch exports off-thread with bounded queue + drop-on-overflow.
  - **Action**: tune via env — `OTEL_BSP_MAX_QUEUE_SIZE`, `OTEL_BSP_MAX_EXPORT_BATCH_SIZE`, `OTEL_BSP_EXPORT_TIMEOUT`. `SimpleSpanProcessor` is **only** allowed in `tests/` (see `tests/unit/conftest.py`).

- **Rule: Propagate context across `asyncio.create_task` and thread pools.** OTEL stores the active span in a contextvar; `asyncio.create_task` does **not** automatically copy contextvars to the child task.
  - **Action**: spans that span async task boundaries must capture `parent_ctx = trace.set_span_in_context(span)` and pass `context=parent_ctx` into `start_as_current_span` inside the child task — exactly the pattern used in `chat_stream`'s `StreamingResponse` generator. Never use `trace.use_span()` across an async-context boundary; it raises "Failed to detach context".

- **Rule: Flush on shutdown.** App lifespan / worker shutdown **must** call `provider.shutdown()` (or rely on `shutdown_tracing()`). Without it, in-flight spans are dropped on SIGTERM and the OTLP connection leaks half-open.

- **Rule: Never touch OTEL private internals from production code.** `_TRACER_PROVIDER_SET_ONCE`, `_TRACER_PROVIDER`, `ProxyTracer._real_tracer` may only be manipulated in `tests/` fixtures or in the single `shutdown_tracing()` helper. Production code uses only the public API.

---



## Third-Party API

This section documents all external API request and response samples including LLM API, Embedding API, Rerank API ...

#### Embedding API

**Endpoint:** `EMBEDDING_API_URL` (default: `http://{embed_base_url}/text_embedding`)
**Timeout:** 60s | **Retry:** 3x @ 1.0s backoff

**Request:**
```json
{
  "texts": ["text1", "text2"],
  "model": "bge-m3",
  "encoding-format": "float"
}
```


** Response Format:**
```json
{
  "returnCode": 96200,
  "returnMessage": "success"
  "returnData": [
    {"index": 0, "embedding": [0.1, 0.2, ...]},
    {"index": 1, "embedding": [0.3, 0.4, ...]}
  ]
}
```

---

#### LLM API

**Endpoint:** `LLM_API_URL` (default: `http://{llm_base_url}/gpt_oss_120b/v1/chat/completions`)
**Timeout:** 120s | **Retry:** 3x @ 2.0s backoff

**Request:**
```json
{
  "model": "gptoss-120b",
  "messages": [
     {"role": "system", "content": "system prompt"},
     {"role": "user", "content": "user input"}
  ],
  "max_tokens": 4096,
  "stream": true,
  "temperature": 0.0
}
```

**Response:**
```json
{
  "model": "gptoss-120b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Under a moonlit sky, the silver‑mane unicorn whispered a lullaby of starlight, gently guiding the sleepy forest creatures into sweet dreams.",
                "reasoning_content": "The user wants a one-sentence bedtime story about a unicorn. Simple. Provide a single sentence. Maybe whimsical.",
                "tool_calls": []
            },
            "logprobs": null,
            "finish_reason": "stop",
            "stop_reason": null
}
```

#### Rerank API

**Endpoint:** `REREANK_API_URL` (default: `http://{rerank_url}`)
**Timeout:** 120s | **Retry:** 3x @ 2.0s backoff

**Request:**
```json
{
    "question": "What is TSMC?",
    "documents": ["PS5, or PlayStation 5, is a video ...", "TSMC stands for Taiwan Semiconductor …", "TSMC is headquartered ..."],
    "model": "bge-reranker-base",
    "top_k": 2
} 
```

**Response:**
```json
{
    "returnCode": 96200,
    "returnMessage": "success",
    "returnData": [
        {
            "score": 0.9999051094055176,
            "index": 1
        },
        {
            "score": 0.6183387041091919,
            "index": 2
        }
    ]
}
```

---

#### LLM & Embedding & Re-rank Auth API (Token Exchange)

**Endpoint:** `AI_API_AUTH_URL` (default: `http://{auth-service-url}/auth/api/accesstoken`)
**Timeout:** `AI_API_AUTH_TIMEOUT` (default: 10s) | **Retry:** 3x @ 1.0s backoff

Exchanges J1 tokens for J2 tokens. Supports two modes:
- **Local**: Uses configured J1 token from `AI_LLM_API_J1_TOKEN` or `AI_EMBEDDING_API_J1_TOKEN` or `AI_RERANK_API_J1_TOKEN`
- **Kubernetes**: Uses service account token from `/var/run/secrets/kubernetes.io/serviceaccount/token` when `AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true`

**Request:**
```json
{
  "key": "j1-token-value"
}
```

**Response:**
```json
{
  "token": "j2-token-value",
  "expiresAt": "2026-01-07T13:20:36Z"
}
```

The `TokenManager` caches J2 tokens and refreshes them 5 minutes before expiration.

---

---

#### HR API

**Endpoint:** `HR_API_URL` (default: `http://hr-service:8080/v3/employees`)
**Timeout:** `HR_API_TIMEOUT` (default: 10s) | **Retry:** 3x @ 0.5s backoff
**Auth:** `Authorization` header with `HR_API_TOKEN` value

**Request:**
```
GET {HR_API_URL}/{employee_key}
Authorization: {HR_API_TOKEN}
```

**Response (200 OK):**
```json
{
  "employee_id": "EMP123",
  "employee_name": "张三",
  "employee_name_en": "Zhang San",
  "org_code": "ORG001"
}
```

**Response (404 Not Found):** Employee not found (valid business response)

---

#### OpenFGA API

**Endpoint:** `OPENFGA_API_URL` (default: `http://openfga:8081`)
**Timeout:** 10s | **Retry:** 3x @ 0.5s backoff
**Auth:** `gam-key` header with `OPENFGA_API_TOKEN` value

**List Resources Request:**
```
POST {OPENFGA_API_URL}/list_resource
gam-key: {OPENFGA_API_TOKEN}
```
```json
{
  "userId": "employee_id_123",
  "relation": "can_view",
  "resourceType": "kms_page"
}
```

**List Resources Response:**
```json
["doc-1", "doc-2", "doc-3"]
```

**Check Permission Request:**
```
POST {OPENFGA_API_URL}/check
```
```json
{
  "userId": "employee_id_123",
  "relation": "can_view",
  "resourceType": "kms_page",
  "resourceId": "doc-1"
}
```

**Check Permission Response:**
```json
{
  "allowed": true
}
```

---

# Command

**Always** run these commands before commit.

## Docker (required for testcontainers integration tests)

Before running `uv run pytest`, ensure the Docker daemon is running:

```bash
# 1. Check if Docker is running
docker ps &>/dev/null && echo "Docker ready" || {
    # 2. Start daemon in background if not running
    sudo dockerd --host=unix:///var/run/docker.sock &>/tmp/dockerd.log &
    # 3. Wait until socket is available (timeout after 30 s to avoid hanging)
    for i in {1..30}; do docker ps &>/dev/null && break; sleep 1; done
    docker ps &>/dev/null || { echo "Docker daemon failed to start within 30s — check /tmp/dockerd.log"; exit 1; }
    echo "Docker daemon started"
}
```

> **Why:** testcontainers (MariaDB, ES, Redis, MinIO) require a live Docker socket.
> Without it, all `@pytest.mark.docker` tests are skipped and remain `[ ]` in plan.md.

## Python

**(Mandatory) Full pre-commit sequence — no commit is valid unless every step is green:**

```bash
# 0. Start Docker daemon FIRST (see Docker section above) — required before pytest
docker ps &>/dev/null || { sudo dockerd --host=unix:///var/run/docker.sock &>/tmp/dockerd.log & for i in {1..30}; do docker ps &>/dev/null && break; sleep 1; done; docker ps &>/dev/null || { echo "Docker daemon failed to start within 30s"; exit 1; }; }

# 1-4. Quality gate (use `make` so pre-commit and CI run identical commands, incl. coverage)
make format
make lint
make test   # MUST include @pytest.mark.docker integration tests — never skip; enforces --cov-fail-under=92
uv run bandit -r src/ --severity-level high --confidence-level high
```

**Enforcement rules:**
- All commands must exit 0 before `git commit`.
- **Start the Docker daemon in advance.** `pytest` must run the **full suite including docker testcontainers integration tests** (`@pytest.mark.docker`). Skipping (via `-m "not docker"`, `--deselect`, env flags, or a missing daemon) is **forbidden**.
- Verify `pytest` output reports **0 skipped** for `@pytest.mark.docker` tests. If any docker test is skipped, fix the daemon and re-run — do not commit.
- Committing with only unit tests green (docker tests skipped) is a process violation; the CI failure that follows is the direct consequence.
