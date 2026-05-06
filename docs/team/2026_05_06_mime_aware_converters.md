## Discussion: MIME-aware converter routing in the ingest pipeline (Round 2 follow-up)

### Master's Opening

**Triggered rules / context**
- Round 1 (`docs/team/2026_05_06_inline_content_ingest.md`) deferred this exact item: "If we ever want clean text from HTML/MD, that's a separate `[BEHAVIORAL]` round." Today's round opens it.
- Current pipeline (`src/ragent/pipelines/factory.py:248`) uses a **single** `TextFileToDocument` for **all four** allowed MIMEs. Effect:
  - `text/markdown` chunks contain literal `#`, `**`, `[link](...)` tokens — embedded and BM25-indexed verbatim.
  - `text/html` chunks contain raw `<div>`, `<script>`, `<style>` markup, navigation chrome, footers — embedded and BM25-indexed verbatim.
  - `text/plain` and `text/csv` already correct (CSV gets line-mode chunking via `_select_profile` at `factory.py:70`).
- Pinned dep: `haystack-ai>=2.7,<3` (`pyproject.toml:10`). Stdlib in this version provides `FileTypeRouter`, `TextFileToDocument`, `MarkdownToDocument`, `HTMLToDocument`, `CSVToDocument` — but the last two require optional Python deps (`trafilatura`, `markdown-it-py`).
- Haystack 2.x convention: `FileTypeRouter` reads `ByteStream.meta["mime_type"]` (no magic-byte sniffing), so the worker MUST already be constructing `ByteStream(data, meta={"mime_type": content_type})` — Round 1 task **T2.17** flagged this as needing confirmation. **This round depends on T2.17 being green.**

**Why now (Pro for opening the round)**
1. Retrieval quality regression: HTML/Markdown noise is in the embedding space and the BM25 index right now. Every `/chat` query competes against `<style>` tokens and `**bold**` markers.
2. Inline-ingest (Round 1) makes HTML/Markdown traffic *more* likely (plugins/MCP tools forwarding rendered Confluence/Notion HTML directly).
3. Cheap to do: Haystack stdlib components, ~40 LOC pipeline rewiring, 2 dep additions. No schema, no API, no worker contract change.

**TIDY-FIRST split (mandatory before voting)**
- **STRUCTURAL** sub-step: extract converter wiring into a dedicated `_build_converter_branch(...)` helper in `pipelines/factory.py`, **still using `TextFileToDocument` only**. Run all existing tests green. Commit `[STRUCTURAL]`.
- **BEHAVIORAL** sub-step: replace the single converter with `FileTypeRouter` + per-MIME converters. Add Red tests first. Commit `[BEHAVIORAL]`.
- These are **separate commits**, per `CLAUDE.md` "Never mix structural and behavioral changes in the same commit."

### Role Perspectives (One Line Each)

- 🏗 **Architect**:
  [Pro] `FileTypeRouter` is the canonical Haystack 2.x indexing pattern; aligns the codebase with the framework's intended graph (router → branches → joiner) and makes future MIMEs (PDF/PPTX/DOCX) a one-line `add_component` rather than a re-architecture.
  [Con] Adds a `DocumentJoiner` (or fan-in via Haystack's auto-merge on equal sockets) before `DocumentCleaner`; pipeline graph grows from 5 to 7+ components. Mitigated by the TIDY-FIRST split — review surface stays small.

- ✅ **QA**:
  [Pro] Behavior is observable and unit-testable per branch: feed a markdown ByteStream → assert `<h1>` text appears without `#`; feed HTML with `<script>alert()</script>` → assert script content is gone; feed CSV → assert per-row Documents (currently `TextFileToDocument` produces ONE Document for the whole CSV — this is actually a **silent bug**, the CSV chunking only works because `_segment("line")` splits by `\n`, but BOM/quoted-newline rows break it).
  [Con] Three new test fixtures (markdown sample, HTML sample with chrome, CSV with quoted commas) and a fixture for the **unclassified** route (must 4xx loudly, not silently drop — Haystack's default sends to `unclassified` output which we must terminate).

- 🛡 **SRE**:
  [Pro] Output quality ↑ ⇒ retrieval precision ↑ ⇒ fewer chat retries ⇒ lower embed/LLM cost. `trafilatura` is pure-Python, no native deps, no container size shock (~3 MB wheel).
  [Con] `trafilatura` pulls `lxml` (compiled wheel, ~8 MB) and `justext`, plus locale data; first-time worker boot adds ~1.5 s import. Compare to the heartbeat interval — fine. Document in `.env.example` / `docs/00_spec.md` §4.6.

- 🔍 **Reviewer**:
  [Pro] Removes a hidden coupling: `_select_profile` at `factory.py:70` reads `meta["content_type"]` only because the converter doesn't know the MIME. After this round, `FileTypeRouter` carries the MIME explicitly per branch, and CSV's line-mode profile can be expressed by **branch placement** rather than meta-string sniffing — but defer that simplification to a future structural round; don't bundle.
  [Con] `MarkdownToDocument` strips structure aggressively (headings become text, links become `text (url)` by default, code blocks lose fences). For RAG over technical docs (which is most of what ragent ingests, per the Confluence focus in `docs/00_spec.md` §3), losing code-fence boundaries is a **regression** for some queries. Need to configure `extract_code: True` (or equivalent) and add a fixture asserting code blocks survive.

- 📋 **PM**:
  [Pro] Improves chat retrieval quality without touching the chat surface — invisible upgrade from caller perspective. No new env vars required to opt in.
  [Con] Risk of chunk-boundary drift: existing READY documents have chunks built from raw markup; new ingests will have chunks built from cleaned text. Mixed corpus until full re-ingest. Decision needed: do we offer a re-ingest path or accept the mixed state? **Recommend accept** — supersede already replaces docs naturally as callers re-POST; no proactive re-ingest tooling this round.

- 💻 **Dev**:
  [Pro] Implementation is mechanical: 1 router + 3 converters + 1 joiner + connect calls. ~40 LOC delta in `factory.py`. Pyproject adds `markdown-it-py` and `trafilatura`. No worker change (T2.17 already wires `ByteStream.meta["mime_type"]`).
  [Con] Haystack's `FileTypeRouter` requires the MIME list to match exactly the converter branch keys. If we drift between `IngestMime` (the request enum from Round 1) and `FileTypeRouter.mime_types`, an allow-listed MIME could fall through to `unclassified`. Single source of truth: import `IngestMime` from `schemas/ingest.py` into `factory.py` and pass `[m.value for m in IngestMime]` — no string literals.

### Conflict Identification

1. **HTML extractor choice**: `HTMLToDocument` (uses `boilerpy3` by default in Haystack 2.7) vs. configuring it to use `trafilatura` (better at boilerplate stripping) vs. raw `BeautifulSoup`-style strip. Reviewer + SRE prefer `trafilatura`.
2. **Markdown structure preservation**: keep code fences and lists or flatten to prose? Reviewer flagged code-fence loss as a real risk for technical-doc retrieval.
3. **`unclassified` branch handling**: terminate with an exception inside the worker (FAILED with `error_code=PIPELINE_UNROUTABLE`) vs. drop silently vs. fall back to `TextFileToDocument`. QA: must be loud — fail.
4. **Re-ingest of old chunks**: ship a backfill script vs. rely on supersede vs. do nothing. PM: rely on supersede.
5. **CSV bug surfacing**: if `CSVToDocument` reveals that `TextFileToDocument` was mis-handling quoted CSVs all along, do we backport a fix to today's pipeline as a **separate** structural round, or accept it inside this BEHAVIORAL round? Architect + QA: separate — don't bundle bug discovery with feature work.

### Voting Results

| Item | Architect | QA | SRE | Reviewer | PM | Dev | Result |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Open the round (do MIME-aware routing now) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| TIDY-FIRST split: STRUCTURAL helper extraction → BEHAVIORAL routing, **two commits** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| `HTMLToDocument` configured with `extractor_type="DefaultExtractor"` (boilerpy3) **for now**; trade up to trafilatura iff retrieval evals show a gap | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | **Pass 4/6** |
| `MarkdownToDocument` with default config; add fixture asserting code-fence content survives in chunk text (even if fences themselves are stripped) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| `unclassified` route → raise → worker FAILED with `error_code=PIPELINE_UNROUTABLE` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Single source of truth: `FileTypeRouter(mime_types=[m.value for m in IngestMime])` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Rely on supersede for migration; **no** backfill tooling this round | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| If `CSVToDocument` exposes pre-existing CSV bugs, file separate journal entry; do not widen scope | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

**Overall: PASS.** SRE and Reviewer dissented on the HTML extractor item; trade-off recorded below.

### Decision Summary

**Approved scope (Round 2).**

1. **Pyproject deps** (`pyproject.toml`): add `"markdown-it-py>=3,<4"` and `"trafilatura>=1.12,<2"` to runtime deps. (`trafilatura` ships even though we default to `boilerpy3` — it's the documented upgrade path and trivial to switch via config; cost is ~8 MB image size, accepted by SRE.)
2. **STRUCTURAL commit (first):** in `src/ragent/pipelines/factory.py`, extract today's converter wiring into a private `_build_converter_branch(pipeline)` helper that adds `TextFileToDocument` and returns the output socket name. **All existing tests must remain green with no diff in behavior.** Commit prefix `[STRUCTURAL]`.
3. **BEHAVIORAL commit (second):** replace `_build_converter_branch` body with:
   ```
   FileTypeRouter(mime_types=[m.value for m in IngestMime])
     ├─ text/plain    → TextFileToDocument
     ├─ text/markdown → MarkdownToDocument
     ├─ text/html     → HTMLToDocument(extractor_type="DefaultExtractor")
     ├─ text/csv      → TextFileToDocument        # CSV stays on text path;
     │                                              line-mode chunking via _select_profile
     │                                              continues to work unchanged
     └─ unclassified  → _RaiseUnroutable          # tiny @component that raises
   ```
   followed by `DocumentJoiner` → existing `DocumentCleaner` → `_IdempotencyClean` → `_CharBudgetChunker` → `DocumentEmbedder` → `DocumentWriter`. Commit prefix `[BEHAVIORAL]`.
4. **Failure path:** the `_RaiseUnroutable` component raises a typed `UnroutableMimeError`; the worker (`workers/ingest.py`, T3.2b) catches it in TX-B, sets `FAILED` with `error_code=PIPELINE_UNROUTABLE`, runs cleanup, emits `event=ingest.failed reason=pipeline_unroutable`. This mirrors the existing `PIPELINE_TIMEOUT` path (S34, B18).
5. **Worker contract (verified, not changed):** the worker downloads bytes from MinIO and passes a `ByteStream(data, meta={"mime_type": doc.content_type, "document_id": doc.document_id})` into the pipeline's `converter` input. Round 1 task **T2.17** is the gate; this round must not start until T2.17 is `[x]`.
6. **No worker, API, schema, or supersede change.** No new env vars. Single source of truth: `IngestMime` enum (Round 1 §1).
7. **Migration:** existing READY chunks remain as-is (raw markup). Re-POSTs naturally supersede with cleaned chunks. No backfill tooling.

**Accepted trade-offs / risks.**
- **HTML extractor choice (4/6 vote).** Defaulting to `boilerpy3` keeps the change minimal and well-understood; SRE/Reviewer prefer `trafilatura` for higher precision boilerplate removal. Decision: ship `boilerpy3` first, add `INGEST_HTML_EXTRACTOR` env var in a follow-up *only if* an A/B retrieval eval shows a measurable gap.
- **Mixed corpus during transition.** Old documents indexed with raw markup, new with cleaned text — search ranking is non-uniform until callers re-POST. Accepted by PM; supersede converges naturally.
- **Markdown formatting loss.** Bold/italic markers are stripped; code-fence *content* is preserved but fence delimiters are not. QA fixture pins this.
- **`trafilatura` adds `lxml`** (~8 MB compiled wheel). SRE accepts; documents in journal under DOMAIN: pipeline.

### TDD Plan (additions to `docs/00_plan.md`, Track T3)

| ID | Phase | Achieve / Deliver | Depends | Status | Owner |
|---|---|---|---|---|---|
| T3.20 | Structural | Extract converter wiring in `factory.py` into `_build_converter_branch(pipeline) -> str` (returns output socket). Behavior identical; all existing tests green. **Commit `[STRUCTURAL]`.** | T3.2, T2.17 | [ ] | Dev |
| T3.21 | Red | `tests/unit/test_ingest_pipeline_routing.py` — feed `ByteStream` with `meta.mime_type ∈ {plain, markdown, html, csv}`; assert each lands on the right converter (introspect pipeline output sockets or assert via mocks). Bad MIME → `UnroutableMimeError`. Code-fence content survives markdown branch. `<script>` content gone from HTML branch. | T3.20 | [ ] | QA |
| T3.22 | Green | Replace `_build_converter_branch` body with `FileTypeRouter` + per-MIME converters + `DocumentJoiner` + `_RaiseUnroutable`. Wire `IngestMime` enum as the only MIME source. **Commit `[BEHAVIORAL]`.** | T3.21 | [ ] | Dev |
| T3.23 | Red | `tests/integration/test_pipeline_unroutable.py` — pipeline given `mime_type="application/x-not-allowed"` → worker FAILED with `error_code=PIPELINE_UNROUTABLE`, cleanup ran, `event=ingest.failed reason=pipeline_unroutable` logged. (Defense-in-depth: API rejects this at 415, but worker must also be safe if a stale MinIO object survives a config change.) | T3.22 | [ ] | QA |
| T3.24 | Green | Worker exception handler maps `UnroutableMimeError` → `FAILED + PIPELINE_UNROUTABLE` in `workers/ingest.py`. | T3.23 | [ ] | Dev |
| T3.25 | Refactor | Pyproject: add `markdown-it-py`, `trafilatura`. Lockfile refresh. `make check` green. | T3.22 | [ ] | Dev |
| T3.26 | Acceptance | `tests/e2e/test_ingest_html_markdown_clean.py` — POST a Markdown doc with `# H1\n**bold**\n```py\nx=1\n```` → READY → query `/chat` → returned chunks contain `H1`, `bold`, `x=1` text **without** `#`, `**`, ` ``` ` markers. Same for an HTML doc containing `<nav>`, `<script>`, `<main>`. | T3.22, T7.2 | [ ] | QA |

**Spec updates (`docs/00_spec.md`):**
- §3.2 (Pipeline graph): replace the single-converter diagram with the routed graph; mention `FileTypeRouter` keys on `ByteStream.meta["mime_type"]`.
- §3.1 R-rules: add **R-MIME** "Unroutable MIME at worker entry → FAILED with `error_code=PIPELINE_UNROUTABLE`."
- §4.6: no new env vars (HTML extractor choice deferred).
- §B-list: add **B-MIME** "Per-MIME converter branches reduce embedding/BM25 noise from markup."

**Journal updates (`docs/00_journal.md`, DOMAIN: pipeline):**
- "Haystack `FileTypeRouter` does not sniff content; the worker must set `ByteStream.meta['mime_type']` from the persisted document MIME (DB column), not from any caller-supplied bytes at pipeline time."
- "Default Haystack `MarkdownToDocument` strips fence delimiters but preserves fenced content. If retrieval over code samples regresses, revisit with `extract_code` config (Haystack 2.8+) or a custom code-aware splitter."

### Pending Items

- **Deferred Decision — `INGEST_HTML_EXTRACTOR=trafilatura|boilerpy3` env var.** Trigger: an A/B eval on a representative HTML corpus shows ≥ 5% precision/recall gap at top-k retrieval. Owner: QA.
- **Deferred Decision — drop `meta["content_type"]` lookup in `_select_profile`** in favor of branch-placement (CSV branch carries an explicit `chunker_profile` meta). Trigger: this round green, then a structural follow-up. Owner: Architect.
- **Deferred Decision — backfill / re-ingest tooling for legacy READY chunks.** Trigger: corpus age analysis shows > 30% of chunks predate the BEHAVIORAL commit and chat retrieval quality on those documents lags. Owner: SRE.
- **Spike — verify `CSVToDocument` is a no-op upgrade for our corpus** before any future migration off `TextFileToDocument` for CSV. Owner: Dev.
