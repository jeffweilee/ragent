---
name: onboard-mime-type
description: Add a new ingest file type / MIME to ragent. Use when the user asks to support a new format — e.g. "ingest PDFs", "accept text/csv again", "add docx support", "allow application/json", "onboard a new file type". Codifies the schema enum, splitter contract, byte-decoding constraint, spec/test surface, and TDD discipline already wired into the v2 ingest pipeline.
---

# Onboarding a New Ingest MIME Type

The v2 ingest pipeline routes per `meta["mime_type"]` through a single
`_MimeAwareSplitter` (see `src/ragent/pipelines/factory.py`). Adding a MIME
means touching every place that enumerates the closed allow-list **and**
adding one new splitter that satisfies the atom contract. Read
`docs/00_spec.md` §3.1–§3.2 and `src/ragent/pipelines/factory.py` before
writing code — every helper described here has a real example there.

---

## Step 1 — Classify the source signal: text, structured-text, or binary

Pick the integration shape based on what the bytes are, not what file
extension users will upload.

| Source bytes | Examples | Splitter shape | Decode path |
|---|---|---|---|
| Plain UTF-8 prose | `text/plain` | Stock `DocumentSplitter(split_by="passage")` | `data.decode("utf-8")` (existing) |
| Structured text with markup | `text/markdown`, `text/html`, `text/csv`, `application/json`, `application/xml` | Custom `@component` AST/DOM walker that emits one atom per logical block | `data.decode("utf-8")` (existing) |
| Binary container | `application/pdf`, `application/vnd.openxmlformats-…docx`, images | **Stop** — see Step 1a below | New decode branch required |

### Step 1a — Binary MIMEs need a worker-side decode change

`src/ragent/workers/ingest.py:_run_pipeline` does
`data.decode("utf-8")` before handing bytes to the loader. That assumption
is **load-bearing** for every existing splitter; the loader receives `str`,
not `bytes`. If the new MIME is binary you must:

1. Branch decode on `mime` in the worker (extract bytes via a converter:
   `pypdf` for PDF, `python-docx` for DOCX, etc.) **before** invoking the
   pipeline, OR
2. Change `_TextLoader.run` to accept `bytes | str` and push the converter
   into a new `@component` upstream of `_MimeAwareSplitter`.

Option (2) is the right shape long-term but is a larger change — it widens
the loader contract every existing splitter relies on. Surface this as a
trade-off to the user before picking; do not silently choose.

The UTF-8 fallback (`errors="replace"` + replacement-count log) and
`magnitude_zero` guard rails (B-rule §00_journal 2026-05-07) only protect
against text corruption, not binary data fed as text. Feeding raw PDF
bytes through the existing path produces a Document full of `�` and
silently embeds garbage.

---

## Step 2 — Confirm the splitter atom contract

Every Document emitted by a splitter MUST satisfy these (spec §3.2,
"Splitter atom contract"):

| Field | Required value | Why |
|---|---|---|
| `content` | Normalized prose text (markup stripped) | Goes to embedder + BM25 — syntax noise hurts recall |
| `meta["raw_content"]` | The source-format markup for that atom — rendered / re-serialized output is what existing splitters produce (`MarkdownRenderer().render(tok)`, `node.html`). Spec §3.2 calls this "exact byte slice"; in practice it's parser-faithful, not byte-identical. | Citation rendering / `_BudgetChunker` raw assembly |
| `meta["mime_type"]` | The routed mime (pass-through from input doc) | Downstream metrics + retry idempotency |
| `meta["document_id"]` | Pass-through | `_BudgetChunker` groups by this; `split_id` resets per doc |
| `meta["source_*"]` | Pass-through (`source_url`, `source_title`, `source_app`, `source_meta`) | Hydrator surfaces these in chat citations |

**Atom granularity** — emit one atom per **smallest never-split unit**, not
per chunk. `_BudgetChunker` packs atoms into ≤ `CHUNK_TARGET_CHARS` chunks
afterward. Anti-patterns:

- Pre-chunking inside the splitter (duplicates `_BudgetChunker`'s job and
  bypasses the overlap/budget invariants).
- Emitting one atom per source document (defeats the splitter — chunker
  hard-splits and loses semantic boundaries).
- Forgetting `raw_content` (the budget chunker falls back to `content` and
  citations lose the original markup; existing splitters all set this
  explicitly).

Block-type whitelist pattern (mistletoe / selectolax precedent):
```python
_BLOCK_TYPES = ("Heading", "Paragraph", "CodeFence", "List", "Table", ...)
# walk → for each block type in whitelist → emit atom with raw=renderer(tok)
```

Your splitter is `byte-stable` (R4/S25): same input bytes ⇒ same atoms,
same order. This is what makes `DuplicatePolicy.OVERWRITE` retry-safe.

---

## Step 3 — The five-site update map

Every MIME is enumerated in five places. Missing any one is a silent
half-onboard:

| Site | File | What changes |
|---|---|---|
| Closed enum (API gate) | `src/ragent/schemas/ingest.py::IngestMime` | Add a `StrEnum` member — this is what Pydantic enforces |
| Router branch (real enforcement) | `src/ragent/pipelines/factory.py::_MimeAwareSplitter.run` | Add `elif mime == "<new>": out = self._<x>.run([doc])["documents"]`; the `else` raises `PIPELINE_UNROUTABLE` |
| Splitter component | `src/ragent/pipelines/factory.py` | New `@component class _<X>Splitter` constructed in `_MimeAwareSplitter.__init__` |
| Documentation constant | `src/ragent/pipelines/factory.py::ALLOWED_MIMES` | Add to the tuple. **Note**: not imported anywhere — keep in sync as a doc reference, but enforcement is the router branch above |
| Module docstring + spec | `factory.py` module + `_MimeAwareSplitter` class docstrings + `docs/00_spec.md` §3.1 (allow-list line), §3.2 (graph), §4.2 (converter table) | The §3.2 graph block still names `FileTypeRouter` even though the code is `_MimeAwareSplitter` — update both names while you're there, don't faithfully copy the drift |

The router is a single `if/elif` chain by design — `_MimeAwareSplitter`
exists because Haystack's stock `FileTypeRouter` only routes
`ByteStream`/`Path`, not `Document` (00_journal 2026-05-07). Don't try to
revive `FileTypeRouter`.

No DB migration is needed: `documents.mime_type` is `VARCHAR(64) NULL`
(migration `004_documents_mime_type.sql`) and stores any allow-listed
string. The metric label is bounded by `IngestMime` enum membership at
the API edge.

---

## Step 4 — Cardinality check before shipping

`pipeline_outcome_total` and `pipeline_duration_seconds` carry
`(source_app, mime_type, outcome)`. Outcomes are `success`/`failed` (2).
Confirm `|source_app| × |IngestMime| × 2` stays ≤ ~200 per metric (rule
from `onboard-business-metric` §Step 2): at 5 × 4 × 2 = 40 a 5th MIME
takes you to 50 — fine. If already over, push back before adding.

---

## Step 5 — Mandatory TDD sequence

Per `CLAUDE.md`, every MIME ships Red → Green → Refactor. **Ordering
constraint**: the `IngestMime` enum, `ALLOWED_MIMES`, the router `elif`,
and `_MimeAwareSplitter.__init__` construction must ship in **one
commit**. Adding the enum alone makes `POST /ingest` accept the new mime
(Pydantic now validates it) and the worker fails with `PIPELINE_UNROUTABLE`
before the splitter exists — that's an observable status=FAILED regression
between commits. For binary MIMEs (Step 1a) the worker decode change rides
in the same commit, otherwise the router routes binary bytes through
`data.decode("utf-8", errors="replace")` and the splitter receives
`�`-mangled garbage that still embeds successfully.

Two commits, in this order:

1. **[BEHAVIORAL] New splitter component + unit tests** — Red: write
   `tests/unit/test_<format>_ast_splitter.py` mirroring
   `test_markdown_ast_splitter.py` / `test_html_ast_splitter.py`. Cover:
   atom emission per block type, `raw_content` shape, empty input,
   oversize input (single atom > `CHUNK_MAX_CHARS` — `_BudgetChunker`
   hard-splits, your splitter must not). Green: implement the `@component`
   class **at module scope only**, not wired into `_MimeAwareSplitter`.
   This commit is purely additive — no observable behavior change.

2. **[BEHAVIORAL] Wire the new MIME end-to-end** — Red: extend
   `tests/unit/test_ingest_request_schema_v2.py::test_ingest_mime_enum_values`
   + a happy-path schema test; extend
   `tests/unit/test_pipeline_routing_v2.py` with
   `test_<format>_routes_to_<format>_splitter`; if onboarding
   `application/pdf`, swap the example in
   `test_unknown_mime_raises_pipeline_unroutable` to a still-unsupported
   MIME (e.g. `image/png`); same for `text/csv` and the schema/router
   negative tests in Step 6. Green: in one commit add the `IngestMime`
   member, append to `ALLOWED_MIMES`, construct the splitter in
   `_MimeAwareSplitter.__init__`, add the `elif` branch, **and** (binary
   only) branch worker decode in `workers/ingest.py:_run_pipeline`. Update
   the `factory.py` module + `_MimeAwareSplitter` class docstrings and
   `docs/00_spec.md` §3.1 / §3.2 / §4.2 in the same commit — they
   describe the new behavior.

3. **Verify** —
   ```bash
   uv run pytest tests/unit/test_ingest_request_schema_v2.py \
                 tests/unit/test_pipeline_routing_v2.py \
                 tests/unit/test_<format>_ast_splitter.py -q
   uv run pytest tests/unit/test_pipeline_factory_unified.py -q   # smoke the full graph
   uv run pytest tests/integration/test_pipeline_retry_idempotent.py -q   # if not docker-gated locally
   ```
   Then `make check`.

Commit discipline (CLAUDE.md "Tidy First") — both commits are
`[BEHAVIORAL]`. Do **not** split the wire-up commit by file type; the
enum, allow-list, router, splitter wiring, and (for binary) worker decode
are one atomic flip. Splitting them produces a window where the API
accepts a mime that doesn't route. The only `[STRUCTURAL]` work that
plausibly fits this skill is post-merge cleanup (renaming a helper,
extracting shared block-walk code) and ships separately.

---

## Step 6 — Negative-test maintenance

Two existing tests pin the closed-enum invariant by example:

| Test | Currently asserts | If your new MIME is… |
|---|---|---|
| `test_ingest_request_schema_v2.py::test_unknown_mime_rejected` | `image/png` rejected | leave alone (image still rejected) |
| `test_ingest_request_schema_v2.py::test_csv_mime_rejected_in_v2` | `text/csv` rejected | **update or delete** if onboarding `text/csv`; otherwise leave alone |
| `test_pipeline_routing_v2.py::test_unknown_mime_raises_pipeline_unroutable` | `application/pdf` raises `PIPELINE_UNROUTABLE` | **change example** if onboarding `application/pdf` |

Likewise `test_ingest_router_v2.py` has `test_post_ingest_unknown_mime_returns_415`
(`image/png`) and `test_post_ingest_csv_mime_returns_415_in_v2`. The same
rule applies — only touch if your new MIME is the one the test uses as
its negative example.

The drift test `tests/unit/test_env_example_drift.py` does NOT gate MIME
additions (no env var is added — the allow-list is in code), but
`tests/integration/test_schema_drift.py` runs `mysqldump` against
`alembic upgrade head`. No schema change here means it stays green.

---

## Step 7 — Reverse-onboarding (removing or deprecating a MIME)

If the user asks to drop a MIME (CSV in v2 was the precedent — see
00_journal 2026-05-07):

1. Remove the enum member, the `ALLOWED_MIMES` entry, the router branch,
   and the splitter class **in one [BEHAVIORAL] commit**.
2. Add a negative test (`test_<mime>_mime_rejected_in_<vN>`) asserting
   `POST /ingest` returns 415 `INGEST_MIME_UNSUPPORTED` for the dropped
   value, so a future PR reintroducing the enum member fails loudly.
3. Update the spec line in §3.1 and the §3.2 graph.
4. Existing `documents` rows with `mime_type='<dropped>'` are NOT migrated
   — they stay in their terminal state (READY/FAILED). Document this in
   the commit message; no backfill is needed because the column is
   metric-bound, not behavior-bound.

Don't deprecate by leaving the enum member and adding a runtime guard —
that's two sources of truth and the metric label cardinality stays high.

---

## Quick checklist (paste into the PR description)

- [ ] Source classified: text / structured-text / binary (binary requires worker decode change — Step 1a)
- [ ] Splitter satisfies atom contract: `content` normalized, `raw_content` is the source-format markup (renderer/reserializer output OK), mime + source meta passed through, byte-stable
- [ ] Five-site update complete: `IngestMime` enum, router `elif`, splitter `@component` + `__init__` construction, `ALLOWED_MIMES` doc constant, factory module + class docstrings
- [ ] Spec updated: `docs/00_spec.md` §3.1 allow-list line, §3.2 graph (also fix the stale `FileTypeRouter` name while there), §4.2 converter table
- [ ] New unit test `test_<format>_ast_splitter.py` covers block-type emission, `raw_content` shape, empty + oversize input
- [ ] `test_pipeline_routing_v2.py` extended with happy-path routing test; if onboarded MIME was the prior `application/pdf` "unknown" example, the negative example is updated to a still-unsupported one
- [ ] Schema enum test (`test_ingest_mime_enum_values`) updated; remaining unknown / image / (CSV-if-not-onboarded) negatives still pass
- [ ] Cardinality math: `|source_app| × |IngestMime| × 2` ≤ ~200 series per metric
- [ ] Two `[BEHAVIORAL]` commits: (1) splitter `@component` alone, (2) atomic wire-up (enum + allow-list + router + `__init__` + binary-decode + docstrings + spec). No mid-state where API accepts a mime that doesn't route.
- [ ] `uv run pytest tests/unit -q` green; `make check` green
- [ ] No DB migration added (the `documents.mime_type` column is open `VARCHAR(64)` — bound at the API enum, not the schema)
