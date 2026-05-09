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
| `meta["raw_content"]` | **Exact source byte slice** for the atom (markup preserved) | Citation rendering / `_BudgetChunker` raw assembly |
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
| Closed enum | `src/ragent/schemas/ingest.py::IngestMime` | Add a `StrEnum` member |
| Pipeline allow-list | `src/ragent/pipelines/factory.py::ALLOWED_MIMES` | Add to the tuple |
| Router branch | `src/ragent/pipelines/factory.py::_MimeAwareSplitter.run` | Add `elif mime == "<new>": out = self._<x>.run([doc])["documents"]` |
| Splitter component | `src/ragent/pipelines/factory.py` | New `@component class _<X>Splitter` constructed in `_MimeAwareSplitter.__init__` |
| Module docstring + spec | `factory.py` header + `docs/00_spec.md` §3.1, §3.2, §4.2 | Update the MIME allow-list line and the routing diagram |

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

The `pipeline_outcome_total` and `pipeline_duration_seconds` metrics are
labeled `(source_app, mime_type, outcome)`. Adding one MIME multiplies
total series by `1 + 1/N` per source_app. Confirm:

```
new_series = |source_app allow-list| × |IngestMime| × |outcome enum|
```

stays under ~200 per metric (per `onboard-business-metric` §Step 2). If
you're at 5 × 4 × 3 = 60, adding a 5th MIME ⇒ 75 — fine. If you're
already over, push back before adding.

---

## Step 5 — Mandatory TDD sequence

Per `CLAUDE.md`, every MIME ships Red → Green → Refactor with structural /
behavioral commits split. Suggested order minimizes broken-state windows:

1. **[STRUCTURAL] Schema enum + tests** — Red: extend
   `tests/unit/test_ingest_request_schema_v2.py::test_ingest_mime_enum_values`
   to assert the new enum value; add a happy-path inline-validates test for
   it. Green: add the `IngestMime` member. The existing
   `test_unknown_mime_rejected` (uses `image/png`) and
   `test_csv_mime_rejected_in_v2` should still pass — if they don't, you
   accidentally added the wrong value.

2. **[BEHAVIORAL] New splitter component + unit tests** — Red: write
   `tests/unit/test_<format>_ast_splitter.py` mirroring
   `test_markdown_ast_splitter.py` / `test_html_ast_splitter.py`. Cover:
   atom emission per block type, `raw_content` byte-stability, empty
   input, oversize input (single atom > `CHUNK_MAX_CHARS` — `_BudgetChunker`
   hard-splits, your splitter must not). Green: implement the
   `@component` class. **Don't** wire it into `_MimeAwareSplitter` yet —
   keep this commit free of routing changes.

3. **[BEHAVIORAL] Wire the router** — Red: extend
   `tests/unit/test_pipeline_routing_v2.py` with
   `test_<format>_routes_to_<format>_splitter`. The
   `test_unknown_mime_raises_pipeline_unroutable` test uses
   `application/pdf` as its "unknown" example — if PDF is what you're
   onboarding, change the assertion to a different unsupported MIME (e.g.
   `image/png`) in the same commit, and call this out in the commit
   message. Green: add to `ALLOWED_MIMES` and the `elif` branch; construct
   the splitter in `_MimeAwareSplitter.__init__`.

4. **[BEHAVIORAL] Worker decode (binary MIMEs only)** — see Step 1a. Skip
   for text-based MIMEs.

5. **[STRUCTURAL] Docs** — update `docs/00_spec.md` §3.1 MIME allow-list
   line, the §3.2 routing diagram, §4.2 converter table; update the
   `factory.py` module docstring. Drift between code and spec is a
   review-blocking finding (`/review` Step 8).

6. **Verify** —
   ```bash
   uv run pytest tests/unit/test_ingest_request_schema_v2.py \
                 tests/unit/test_pipeline_routing_v2.py \
                 tests/unit/test_<format>_ast_splitter.py -q
   uv run pytest tests/unit/test_pipeline_factory_unified.py -q   # smoke the full graph
   uv run pytest tests/integration/test_pipeline_retry_idempotent.py -q   # if not docker-gated locally
   ```
   Then `make check`.

Commit discipline (CLAUDE.md "Tidy First"):
- `[STRUCTURAL]` — enum addition (it doesn't change behavior until the
  router branch is wired), docstring/spec updates.
- `[BEHAVIORAL]` — splitter implementation, router branch, worker decode.
- **Never mix** in the same commit. Splitter implementation + router wiring
  is two commits even though they ship together — they fail differently
  and review separately.

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
2. Add a positive negative-test (`test_<mime>_mime_rejected_in_<vN>`) so a
   future PR reintroducing the enum value fails loudly.
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
- [ ] Splitter satisfies atom contract: `content` normalized, `raw_content` = source byte slice, mime + source meta passed through, byte-stable
- [ ] Five-site update complete: `IngestMime` enum, `ALLOWED_MIMES`, `_MimeAwareSplitter` branch + `__init__`, splitter `@component`, factory module docstring
- [ ] Spec updated: `docs/00_spec.md` §3.1 allow-list line, §3.2 graph, §4.2 converter table
- [ ] New unit test `test_<format>_ast_splitter.py` covers block-type emission, `raw_content` stability, empty + oversize input
- [ ] `test_pipeline_routing_v2.py` extended with happy-path routing test; if onboarded MIME was the prior `application/pdf` "unknown" example, the negative example is updated to a still-unsupported one
- [ ] Schema enum test (`test_ingest_mime_enum_values`) updated; CSV / image / unknown negatives still pass
- [ ] Cardinality math: `|source_app| × |IngestMime| × |outcome|` ≤ ~200 series per metric
- [ ] `[STRUCTURAL]` and `[BEHAVIORAL]` commits split; splitter and router wiring are separate commits
- [ ] `uv run pytest tests/unit -q` green; `make check` green
- [ ] No DB migration added (the `documents.mime_type` column is open `VARCHAR(64)` — bound at the API enum, not the schema)
