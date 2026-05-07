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

### 2. Configure environment

Copy `.env.example` to `.env` and fill in the required values (MariaDB DSN, Redis Sentinel, Elasticsearch, MinIO sites, third-party API URLs). The commands below load this file via `uv run --env-file .env`.

```bash
cp .env.example .env
```

### 3. Run database migrations

```bash
uv run --env-file .env alembic upgrade head
```

### 4. Start the API server

```bash
uv run --env-file .env python -m ragent.api
```

### 5. Start the background worker

```bash
uv run --env-file .env python -m ragent.worker
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

See [`docs/API.md`](docs/API.md) for the full endpoint catalogue (ingest, chat, retrieve, observability, MCP).

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the system diagram and key design decisions.

---

## Docs

| File | Purpose |
|---|---|
| `docs/API.md` | API reference |
| `docs/ARCHITECTURE.md` | Architecture and design decisions |
| `docs/00_rule.md` | Development standards and mandatory workflow |
| `docs/00_spec.md` | Full technical specification |
| `docs/00_plan.md` | TDD implementation checklist |
| `docs/00_agent_team.md` | Agent team and workflow |
| `docs/00_journal.md` | Team reflection and blameless guidelines |
