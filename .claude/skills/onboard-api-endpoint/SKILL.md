---
name: onboard-api-endpoint
description: Add a new versioned API endpoint to ragent. Use when the user asks to add a new route — e.g. "add a GET /documents endpoint", "expose a new action on the chat router", "create a v2 of ingest". Codifies the path naming convention, router-prefix discipline, spec/plan update, and TDD sequence already wired into the project.
---

# Onboarding a New API Endpoint

All ragent business endpoints follow the shape `/<resource>/v<N>[/<rest>]` with
the version segment carried by the **router prefix**, not individual decorators.
Read `docs/00_rule.md §API Endpoint Naming & Versioning` and
`src/ragent/bootstrap/app.py` before touching any router file.

---

## Orientation — how a route is registered end-to-end

```
[ Router factory — src/ragent/routers/<resource>.py ]

  def create_<resource>_router(...) -> APIRouter:
      router = APIRouter(prefix="/<resource>/v1")   # ← version here, once
      @router.post("")                               # ← collection op
      @router.get("/{id}")                           # ← item op
      @router.post("/stream")                        # ← sub-action
      return router

[ Composition root — src/ragent/bootstrap/app.py ]

  app.include_router(create_<resource>_router(...))  # ← wired here

[ Auth middleware — bootstrap/app.py::_NO_USER_ID_PATHS ]

  # If the endpoint must be reachable WITHOUT X-User-Id (health, metrics),
  # add its exact path to _NO_USER_ID_PATHS. Business endpoints omit this.

[ Test contract ]

  tests/unit/test_api_versioning.py   # asserts every non-infra route matches
                                      # r"^/[a-z][a-z0-9-]*/v\d+"
```

Existing routers to read as living references:

| Router | File | Prefix | Routes |
|---|---|---|---|
| Ingest | `src/ragent/routers/ingest.py` | `/ingest/v1` | `""`, `/{document_id}` (GET+DELETE), `""` (GET list) |
| Chat | `src/ragent/routers/chat.py` | `/chat/v1` | `""`, `/stream` |
| Retrieve | `src/ragent/routers/retrieve.py` | `/retrieve/v1` | `""` |
| MCP | `src/ragent/routers/mcp.py` | `/mcp/v1` | `/tools/rag` |
| Health | `src/ragent/routers/health.py` | *(none)* | `/livez`, `/readyz`, `/startupz` |

---

## Step 1 — Classify the operation

Decide which of these three situations applies before writing a single line.

| Situation | Action |
|---|---|
| **New route on an existing resource** (e.g. `POST /ingest/v1/supersede`) | Add a decorator to the existing router factory; no new file |
| **Brand-new resource** (e.g. `GET /documents/v1`) | Create `src/ragent/routers/<resource>.py` with a new factory function; wire it in `app.py` |
| **Breaking change to existing resource** (e.g. different request/response shape) | Create `create_<resource>_v2_router()` with `prefix="/<resource>/v2"`; mount both in `app.py`; do NOT modify the v1 router |

Ask the user which case applies when it is not obvious — do not silently pick.

---

## Step 2 — Name the path correctly

Rules from `docs/00_rule.md §API Endpoint Naming & Versioning`:

- Resource segment: **lowercase, hyphen-separated noun** (`document-revisions`, not `DocumentRevisions` or `document_revisions`).
- Version token: `v` + positive integer, no suffix (`v1`, never `v1-beta`).
- Sub-action segment: lowercase verb or noun that reads as an action on the resource (`/stream`, `/supersede`, `/tools/rag`).
- Collection vs. item:
  - `POST /<resource>/v1` — create (returns 202/201)
  - `GET /<resource>/v1` — list (returns 200 + pagination)
  - `GET /<resource>/v1/{id}` — fetch one (returns 200 or 404)
  - `DELETE /<resource>/v1/{id}` — delete (returns 204)
  - `POST /<resource>/v1/{id}/<action>` — item-scoped action (returns 200/202)

If the endpoint is infrastructure-style (health probe, internal ping), it goes in `health.py` or a dedicated infra router with **no version prefix** and must be added to `_NO_USER_ID_PATHS`.

---

## Step 3 — Spec and plan before code

Before writing tests, update two documents:

1. **`docs/00_spec.md`** — add a row to the endpoint table (§4.1.2 or the
   relevant section) with method, path, auth header, request schema ref,
   response schema ref, and notes. Add a scenario (`S-XX`) if there is a
   behaviour contract worth pinning (happy path, error codes, edge cases).

2. **`docs/00_plan.md`** — append a new task row under the active track:
   ```
   | T-XX.N | Red   | • **Achieve:** Pin <endpoint> contract.<br>• **Deliver:** `tests/unit/test_<resource>_router.py` — <what the test covers>. | pending | [x] | QA |
   | T-XX.N | Green | • **Achieve:** Implement <endpoint>.<br>• **Deliver:** `src/ragent/routers/<resource>.py::<handler>`. | T-XX.N | [ ] | Dev |
   ```

Never write production code before the spec and plan entries exist — the
pre-commit gate's `/simplify` + `/review` cycle will flag the gap.

---

## Step 4 — TDD sequence

Per `CLAUDE.md`, every endpoint ships Red → Green → Refactor.

### Red

Write the failing test first. Minimum test surface for any new route:

```python
# tests/unit/test_<resource>_router.py

def _make_client(...) -> TestClient:
    app = FastAPI()
    app.include_router(create_<resource>_router(...))
    return TestClient(app, raise_server_exceptions=False)

def test_<endpoint>_happy_path():
    client = _make_client(...)
    resp = client.<method>("/<resource>/v1[/<rest>]", json={...})
    assert resp.status_code == <expected>
    # assert response schema fields

def test_<endpoint>_returns_problem_json_on_error():
    ...
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["error_code"] == "<ERROR_CODE>"
```

Run `uv run pytest tests/unit/test_<resource>_router.py -x` — confirm it
fails with 404 (path not found) or an assertion error, not a Python error.

### Green

Implement the minimum handler. Template for a new route on an existing router:

```python
# in create_<resource>_router():
@router.<method>("<relative-path>", status_code=<N>, response_model=<ResponseModel>)
async def <handler>(
    body: <RequestModel>,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> <ResponseModel> | Response:
    try:
        result = await svc.<operation>(...)
    except <DomainError>:
        return problem(<status>, HttpErrorCode.<CODE>, "<message>")
    return <ResponseModel>(...)
```

For a **brand-new resource** also:

1. Create `src/ragent/routers/<resource>.py` following the factory function pattern.
2. Import and wire in `src/ragent/bootstrap/app.py`:
   ```python
   from ragent.routers.<resource> import create_<resource>_router
   # inside create_app():
   app.include_router(create_<resource>_router(svc=container.<resource>_svc))
   ```
3. Confirm `tests/unit/test_api_versioning.py` still passes — it asserts
   every non-infra route carries a version segment.

### Refactor

- Extract repeated error-handling into a shared helper if ≥ 2 routes share the same `except` pattern.
- Ensure all `response_model=` annotations are present (FastAPI OpenAPI schema completeness).
- Run `make check` (format + lint + tests).

---

## Step 5 — Error codes

Every non-2xx response needs a stable `error_code`. Before adding a new one:

1. Check `src/ragent/errors/codes.py::HttpErrorCode` — reuse an existing code if semantics match.
2. Add a new `SCREAMING_SNAKE` member only for genuinely distinct failure modes.
3. Add the new code to the error catalog table in `docs/00_spec.md §4.1.2` with status, path, and owning task.
4. Add a test asserting `resp.json()["error_code"] == HttpErrorCode.<CODE>`.

---

## Step 6 — Versioning a breaking change (`v2`)

Only do this when the request or response shape changes in a way that
breaks existing clients (field removed, type changed, semantics inverted).

```python
# src/ragent/routers/ingest.py (example)

def create_ingest_v2_router(svc: Any) -> APIRouter:
    router = APIRouter(prefix="/ingest/v2", route_class=_IngestRoute)
    # new shapes here
    return router
```

```python
# src/ragent/bootstrap/app.py
app.include_router(create_ingest_router(svc=ingest_svc))      # v1 stays live
app.include_router(create_ingest_v2_router(svc=ingest_svc))   # v2 alongside
```

Document the deprecation timeline for v1 in `docs/00_spec.md` and a
`docs/00_plan.md` decommission task before merging the v2 router.

---

## Step 7 — Auth middleware bypass (infra-only)

If the new endpoint must be reachable **without** `X-User-Id` (a health
probe, a metrics scrape, an unauthenticated callback), add its path to
`_NO_USER_ID_PATHS` in `src/ragent/bootstrap/app.py` and to the
`_SKIP_PATHS` list in `src/ragent/middleware/logging.py`. Business
endpoints (any path under `/<resource>/v<N>`) must NOT be in this list.

---

## Quick checklist (paste into the PR description)

- [ ] Situation classified: new route on existing router / new resource / v2 bump
- [ ] Path follows `/<resource>/v<N>[/<rest>]` naming convention; version in router prefix only
- [ ] `docs/00_spec.md` endpoint table row + scenario(s) added
- [ ] `docs/00_plan.md` Red + Green task rows added
- [ ] Failing test written first; confirmed to fail with 404 or assertion before any production code
- [ ] `response_model=` annotation on every new route decorator
- [ ] Non-2xx paths covered by tests asserting `error_code` in response body
- [ ] New `HttpErrorCode` members (if any) added to `src/ragent/errors/codes.py` and spec error catalog
- [ ] For new resource: `create_<resource>_router()` wired in `bootstrap/app.py`
- [ ] `tests/unit/test_api_versioning.py` still passes after wiring
- [ ] For v2: old version still mounted; deprecation timeline in spec; decommission task in plan
- [ ] Infra bypass: only added to `_NO_USER_ID_PATHS` if genuinely infra (not a business endpoint)
- [ ] `make check` green (format + lint + full test suite)
- [ ] `[BEHAVIORAL]` commit; no structural changes mixed in
