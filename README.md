# ragent

RAG backend — ingest, hybrid retrieval, chat.

---

## Quick Start

Prerequisites: Python ≥ 3.12, `uv`, MariaDB 10.6, Redis (Sentinel), Elasticsearch 9.2.3, MinIO.

```bash
uv sync                                                  # install dependencies
cp .env.example .env                                     # then edit .env to fill in DSNs, MinIO sites, API URLs
make doctor                                              # pre-flight check (env + datastores + AI endpoints)
uv run --env-file .env alembic upgrade head              # run database migrations
uv run --env-file .env uvicorn ragent.bootstrap.app:create_app --factory --host "${RAGENT_HOST:-0.0.0.0}" --port "${RAGENT_PORT:-8000}"  # API server
uv run --env-file .env python -m ragent.worker           # background worker (separate shell)
uv run --env-file .env python -m ragent.reconciler       # reconciler process (separate shell)
curl http://localhost:8000/livez                         # verify — expect {"status":"ok"}
make doctor PROBE_LIVE=1                                 # post-launch — also probes /livez and /readyz

uv export --format requirements-txt --no-hashes --dev -o requirements.txt # export requirement.txt
```

### MCP Hub

Standalone FastMCP service that federates arbitrary third-party REST APIs as MCP tools.

```bash
export MCP_HUB_TOOLS_YAML=src/ragent/mcp_hub/tools.example.d   # demo registry
uv run python -m ragent.mcp_hub.doctor                          # validate yaml
uv run uvicorn ragent.mcp_hub.server:build_mcp_app --factory --host "${MCP_HUB_HOST:-0.0.0.0}" --port "${MCP_HUB_PORT:-9000}"  # binds /mcp
```

### Development

```bash
make check        # format + lint + test (Linux / macOS)
make test         # full suite with 92% coverage gate
make test-gate    # unit + integration only (pre-commit gate)
```

---

## Project Structure

```
src/ragent/
  api.py / worker.py / reconciler.py  — three process entrypoints
  bootstrap/        — composition root, app factory, schema init, logging
  routers/          — FastAPI routers: ingest, chat, retrieve, feedback, mcp, health
  services/         — business logic: IngestService
  repositories/     — DB access: DocumentRepository
  pipelines/        — Haystack pipelines: ingest, retrieval
  plugins/          — extractor plugins: VectorExtractor, StubGraphExtractor
  clients/          — 3rd-party clients: EmbeddingClient, LLMClient, RerankClient
  mcp_hub/          — standalone FastMCP hub (separate process)
  storage/          — MinIO site registry
  auth/             — JWT verification, permission deps
  schemas/          — Pydantic request/response models
migrations/         — Alembic SQL + schema.sql snapshot
resources/es/       — Elasticsearch index/pipeline/alias definitions
tests/{unit,integration,e2e}/
docs/               — spec, plan, journal, API reference
```

---

## Docs

| File | Purpose |
|---|---|
| [`docs/API.md`](docs/API.md) | API reference (ingest, chat, retrieve, feedback, observability, MCP) |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System diagram and key design decisions |
| [`docs/00_domain_map.md`](docs/00_domain_map.md) | Domain boundary and quick index |
| [`docs/00_rule.md`](docs/00_rule.md) | Development standards and mandatory workflow |
| [`docs/00_spec.md`](docs/00_spec.md) | Full technical specification (subdocs in `docs/spec/`) |
| [`docs/00_plan.md`](docs/00_plan.md) | Active TDD implementation checklist (completed tracks archived in [`docs/00_plan_done.md`](docs/00_plan_done.md)) |
| [`docs/00_agent_team.md`](docs/00_agent_team.md) | Agent team and workflow |
| [`docs/00_journal.md`](docs/00_journal.md) | Team reflection and blameless guidelines |
