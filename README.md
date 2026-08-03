# ragent

RAG backend — ingest, hybrid retrieval, chat, chat with upstream agent.

---

## Quick Start

Prerequisites: Python ≥ 3.10, `uv`, MariaDB 10.6, Redis, Elasticsearch 9.2.3, MinIO.

```bash
uv sync                                                  # install dependencies, keep uv.lock package version while replacing uv index url
cp .env.example .env                                     # then edit .env to fill in DSNs, MinIO sites, API URLs
make doctor                                              # pre-flight check (env + datastores + AI endpoints)
uv run --env-file .env alembic upgrade head              # run database migrations
uv run --env-file .env uvicorn ragent.bootstrap.app:create_app --factory --host "${RAGENT_HOST:-0.0.0.0}" --port "${RAGENT_PORT:-8000}"  # API server
uv run --env-file .env python -m ragent.worker           # background worker (separate shell)
uv run --env-file .env python -m ragent.reconciler       # reconciler process (separate shell)
curl http://localhost:8000/livez                         # verify — expect {"status":"ok"}
make doctor PROBE_LIVE=1                                 # post-launch — also probes /livez and /readyz

uv export --format requirements-txt --no-hashes --dev -o requirements.txt # export requirement.txt

make check        # format + lint + mcp-hub-check + test (Linux / macOS)
make test         # full suite with 92% coverage gate
make test-gate    # unit + integration only (pre-commit gate)
```

MCP Hub (federates third-party REST APIs as MCP tools) is a separate top-level package with its own setup — see [`docs/mcp_hub.md`](docs/mcp_hub.md).

---

## Project Structure

```
src/ragent/
  api.py / worker.py / reconciler.py  — three process entrypoints
  bootstrap/        — composition root, app factory, schema init, logging
  routers/          — FastAPI routers: ingest, chat, retrieve, feedback, mcp, health, admin
  services/         — business logic: IngestService, embedding lifecycle
  repositories/     — DB access: DocumentRepository, FeedbackRepository
  pipelines/        — Haystack pipelines: ingest, retrieval
  extractors/       — pluggable extractors: VectorExtractor, StubGraphExtractor
  workers/          — TaskIQ task entrypoints: ingest, backfill, heartbeat, maintenance, startup sweep
  clients/          — 3rd-party clients: EmbeddingClient, LLMClient, RerankClient
  storage/          — MinIO site registry
  auth/             — JWT verification, permission deps
  middleware/       — request logging, TaskIQ context propagation
  security/         — archive (zip bomb) guard
  errors/           — error codes, RFC 9457 problem details
  utility/          — env/datetime helpers, feedback token HMAC
  schemas/          — Pydantic request/response models
migrations/         — schema.sql snapshot
alembic/            — Alembic upgrade/downgrade SQL + env.py
resources/es/       — Elasticsearch index/pipeline/alias definitions
mcp_hub/            — standalone FastMCP hub package (separate process, own pyproject.toml)
tests/{unit,integration,e2e}/
docs/               — spec, plan, journal, API reference
```

---

## Docs

| File | Purpose |
|---|---|
| [`docs/00_API.md`](docs/00_API.md) | API reference (ingest, chat, retrieve, feedback, observability, MCP) |
| [`docs/00_ARCHITECTURE.md`](docs/00_ARCHITECTURE.md) | System diagram and key design decisions |
| [`docs/00_domain_map.md`](docs/00_domain_map.md) | Domain boundary and quick index |
| [`docs/00_api_call_chains.md`](docs/00_api_call_chains.md) | API surface call chains |
| [`docs/00_rule.md`](docs/00_rule.md) | Development standards and mandatory workflow |
| [`docs/00_spec.md`](docs/00_spec.md) | Full technical specification (subdocs in `docs/spec/`) |
| [`docs/00_plan.md`](docs/00_plan.md) | Active TDD implementation checklist (completed tracks archived in [`docs/00_plan_done.md`](docs/00_plan_done.md)) |
| [`docs/00_agent_team.md`](docs/00_agent_team.md) | Agent team and workflow |
| [`docs/00_journal.md`](docs/00_journal.md) | Team reflection and blameless guidelines |
| [`docs/mcp_hub.md`](docs/mcp_hub.md) | MCP Hub design and setup (separate package) |
