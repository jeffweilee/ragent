## Discussion: Ingest API consuming `inline_content` (JSON body, no multipart upload)

### Master's Opening

**Triggered rules / context**
- Spec §3.1 / §4.1: today `POST /ingest` is **multipart-only** (`UploadFile` + form fields) — see `src/ragent/routers/ingest.py:21-63`.
- Allow-list is small and text-only: `_ALLOWED_MIMES = {text/plain, text/markdown, text/html, text/csv}` (`src/ragent/services/ingest_service.py:14`).
- Haystack 2.x `FileTypeRouter` infers MIME from **path extension** for `Path` inputs and from **`ByteStream.meta["mime_type"]`** for byte streams — there is no built-in magic-byte sniffing. Therefore an inline body API **must** require the caller to declare `content_type` and we must propagate it through MinIO → worker → Haystack `ByteStream` metadata.
- Caller use-cases driving the request: agentic tools, MCP servers, plugins (Confluence/Slack scrapers) that already hold the rendered text/markdown in memory and currently have to fabricate a multipart envelope just to call us.
- TIDY-FIRST: the path is **purely additive** (a new request shape on the same endpoint or a sibling endpoint). No change to worker / pipeline / supersede semantics if we keep the storage contract identical (still write to MinIO, still kiq `ingest.pipeline`).
- YAGNI guardrails: do **not** introduce binary inline ingest (PDF/PPTX) in this round — it conflicts with the current text-only MIME allow-list and with the JSON transport (would force base64 + 33% overhead for the 50 MB ceiling = 67 MB JSON). Scope this round to the same allow-list as multipart.

### Role Perspectives (One Line Each)

- 🏗 **Architect**:
  [Pro] One canonical pipeline (MinIO → worker → Haystack `ByteStream(meta.mime_type)`) regardless of transport keeps supersede, reconciler, and idempotency invariants untouched (spec §3.1, R4).
  [Con] Two transports on one endpoint widens the validation surface (415/413/422 paths must be re-derived for JSON); a sibling path `POST /ingest/inline` is cleaner than overloading `Content-Type` discrimination on `/ingest`.

- ✅ **QA**:
  [Pro] Contract is small and easy to pin in TDD: declared `content_type` ∈ allow-list, `content` non-empty, byte-size ≤ `INGEST_MAX_FILE_SIZE_BYTES`, same `(source_id, source_app, source_title)` mandatory triple — reuses T2.7/T2.13 fixtures.
  [Con] Need explicit Red tests for the new failure modes: `content_type` missing (422 `INGEST_VALIDATION`), declared `text/markdown` but body is binary garbage (we will **not** sniff — accepted risk, must be documented), UTF-8 decode boundary (caller sends `content` as JSON string ⇒ already UTF-8; but raw bytes path needs base64 + size accounting **post-decode**).

- 🛡 **SRE**:
  [Pro] No new datastore, no new worker task, no new metric — `event=ingest.{started,failed,ready}` and `worker_pipeline_duration_seconds` already cover it. Backpressure via existing TaskIQ broker.
  [Con] JSON bodies are buffered fully in the API process before MinIO put — a single 50 MB request doubles RSS vs. streamed multipart. Mitigation: keep `INGEST_MAX_FILE_SIZE_BYTES` ceiling and apply it to the **decoded** size, not the JSON envelope size; reject early via `Content-Length` pre-check.

- 🔍 **Reviewer**:
  [Pro] Surgical change: the ingest **service** already takes `file_data: BytesIO, file_size: int, content_type: str` (`ingest_service.py:create`) — the router can convert `inline_content` into that exact tuple with zero service-layer churn. Matches "no abstraction for single-use logic".
  [Con] Watch for duplication between the multipart and inline router handlers — extract a private `_dispatch_create(...)` helper rather than copy-pasting field validation.

- 📋 **PM**:
  [Pro] Unblocks plugin/MCP authors who hold rendered text in memory; aligns with B11 (plugin extract returns text already) without forcing a synthetic file step.
  [Con] Scope must be capped at text-MIME allow-list this round; binary inline (PDF/PPTX/DOCX) is a **separate follow-up** because it requires (a) base64 transport, (b) widening the allow-list, (c) Haystack converter wiring (`PyPDFToDocument` etc.), and (d) chunker behavior review for non-text inputs. File a deferred ticket; do not bundle.

- 💻 **Dev**:
  [Pro] Implementation footprint ≈ 1 schema + ~30 LOC in router + 1 unit test file. The MinIO put still happens (keeps reconciler R1 intact). No worker change.
  [Con] FastAPI route signature can't simultaneously accept `multipart/form-data` and `application/json` cleanly without a Union body; prefer **a sibling sub-path** `POST /ingest:inline` (or `POST /ingest/inline`) — the colon form is unambiguous and avoids accidentally matching `/ingest/{document_id}`. Dev recommends the slash form `POST /ingest/inline` since the GET/DELETE routes are `/ingest/{document_id}` and `inline` would shadow nothing if registered before the parameterized route.

### Conflict Identification

1. **Endpoint shape** — overload `/ingest` with content-negotiation vs. add sibling `/ingest/inline`.
2. **MIME enforcement** — declared-only (current consensus) vs. add libmagic sniffing as a defense.
3. **Binary inline (base64) in scope?** — Architect/QA/PM say defer; Dev/Reviewer agree.
4. **Size accounting** — JSON envelope size vs. decoded `content` byte length.

### Voting Results

| Item | Architect | QA | SRE | Reviewer | PM | Dev | Result |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Add **sibling endpoint** `POST /ingest/inline` (not overload) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| **Declared-only** MIME (no libmagic this round; document accepted risk) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| **Defer binary/base64 inline** to a later round | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | **Pass 5/6** |
| Enforce `INGEST_MAX_FILE_SIZE_BYTES` against **decoded byte length** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Reuse existing service `create(...)` unchanged; router-only adapter | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

**Overall: PASS.** Master not invoked.

### Decision Summary

**Approved scope (this round only).**

1. **Endpoint:** `POST /ingest/inline` — JSON body — registered **before** the parameterized `/ingest/{document_id}` GET/DELETE routes to avoid path-matching ambiguity. Same headers as `/ingest` (e.g. `X-User-Id`).
2. **Request schema** (`src/ragent/schemas/ingest.py` — new, Pydantic v2):
   ```jsonc
   {
     "source_id":        "DOC-123",            // required
     "source_app":       "confluence",         // required
     "source_title":     "Q3 OKR Planning",    // required
     "source_workspace": "eng",                // optional
     "content_type":     "text/markdown",      // required, ∈ allow-list
     "content":          "# Heading\n..."      // required, non-empty UTF-8 string
   }
   ```
3. **Allow-list (unchanged this round):** `{text/plain, text/markdown, text/html, text/csv}`.
   Reject anything else with `415 INGEST_MIME_UNSUPPORTED` (same error_code as multipart path — single source of truth).
4. **Size limit:** `len(content.encode("utf-8")) ≤ INGEST_MAX_FILE_SIZE_BYTES` (B28, default 50 MB). Over → `413 INGEST_FILE_TOO_LARGE`. The JSON envelope itself gets the standard FastAPI body-size guard.
5. **Validation order (mirrors `routers/ingest.py:30-44`):**
   1. Required fields → 422 `INGEST_VALIDATION` with `errors[]`.
   2. `content_type` allow-list → 415.
   3. Decoded byte size → 413.
   6. Then call `svc.create(file_data=BytesIO(content.encode("utf-8")), file_size=<decoded>, content_type=<declared>, ...)` — **no service change**.
6. **Worker / pipeline:** untouched. The Haystack indexing pipeline must already be wiring `ByteStream(data, meta={"mime_type": content_type})` from MinIO download — confirm in T3.2 implementation; if not, that is a **bug fix** carried in a separate `[STRUCTURAL]` commit, not bundled with the inline feature.
7. **Observability:** reuse `event=ingest.started`; add structured-log field `transport=inline|multipart` so we can dashboard adoption without new metrics.
8. **Auth & permission:** identical to `/ingest` — `X-User-Id` middleware (T7.5c) applies; permission gate (T8.8) does **not** need changes since this is a create path (existing rule: create is unrestricted regardless of `RAGENT_PERMISSION_INGEST_ENABLED`).

**Accepted trade-offs / risks.**
- **No content sniffing:** a caller can declare `text/markdown` and submit bytes that aren't markdown. Pipeline downstream behavior is then undefined; this matches today's multipart contract and Haystack's `FileTypeRouter` semantics. Documented accepted risk; revisit if false-classification incidents appear in journals.
- **Memory pressure on large JSON:** the API process buffers up to ~50 MB per request; mitigated by existing `INGEST_MAX_FILE_SIZE_BYTES` and FastAPI request limit. Not a regression vs. multipart.

### TDD Plan (additions to `docs/00_plan.md`, Track T2)

| ID | Phase | Achieve / Deliver | Depends | Status | Owner |
|---|---|---|---|---|---|
| T2.15 | Red | **Pin inline-ingest schema + validation order.** `tests/unit/test_ingest_router_inline.py` — required-field 422 (`source_id`/`source_app`/`source_title`/`content_type`/`content`); `content_type` outside allow-list → 415; UTF-8-encoded body > `INGEST_MAX_FILE_SIZE_BYTES` → 413; happy path → 202 with `task_id`; service called with `file_data=BytesIO(utf8)`, `file_size=len(utf8)`, `content_type=<declared>`. | T2.13, T2.14 | [ ] | QA |
| T2.16 | Green | **Implement `POST /ingest/inline` adapter** in `src/ragent/routers/ingest.py` (sibling handler; extract private `_create_document(...)` helper to dedupe field validation across multipart/inline). Add `InlineIngestRequest` Pydantic model in `src/ragent/schemas/ingest.py`. | T2.15 | [ ] | Dev |
| T2.17 | Refactor | **Confirm worker passes declared `content_type` into Haystack `ByteStream(meta={"mime_type": ct})`** (`src/ragent/pipelines/factory.py` / `workers/ingest.py`). If already correct → mark `[~]` with note; otherwise STRUCTURAL fix, no behavior change for multipart. | T2.16, T3.2 | [ ] | Dev |
| T2.18 | Acceptance | **E2E:** `tests/e2e/test_ingest_inline_success.py` — POST inline markdown → poll `/ingest/{id}` → READY within 30 s; chunks created in ES; supersede behavior identical to multipart for matching `(source_id, source_app)`. | T2.16, T7.2 | [ ] | QA |

**Spec updates (`docs/00_spec.md`):**
- §3.1 flow step 1: append "or `POST /ingest/inline` with JSON body `{source_id, source_app, source_title, source_workspace?, content_type, content}` — same MIME allow-list, same size ceiling, same downstream pipeline."
- §4.1: add the new path with the schema above.
- §4.6 / B28: no new env var.

### Pending Items

- **Deferred Decision — Binary inline ingest (PDF/PPTX/DOCX, base64).** Trigger: when (a) the multipart MIME allow-list is widened to binary types **and** (b) at least one caller (plugin/MCP) cannot use multipart. Owner: PM to file ticket `INGEST-INLINE-BINARY`.
- **Deferred Decision — libmagic / content sniffing.** Trigger: ≥ 3 incidents of MIME mis-declaration causing pipeline failures; revisit in journal.
- **Reflection note for `docs/00_journal.md` (DOMAIN: ingest):** "Haystack 2.x `FileTypeRouter` does not sniff bytes — it reads `Path` extension or `ByteStream.meta['mime_type']`. Any new ingest transport must therefore require the caller to declare MIME and propagate it into `ByteStream` metadata; otherwise downstream branches (e.g. CSV `RowMerger`) silently mis-route."
