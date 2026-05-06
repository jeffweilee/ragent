## Discussion: Convergence — AST-based chunking + dual-field ES schema

### Master's Opening

**Convergence framing.** Two prior rounds opened the design space; this round closes it. User-mandated convergence:

1. **Inline endpoint scope = three MIMEs:** `text/plain`, `text/markdown`, `text/html`. CSV is **excluded from inline** (escaping/transport mismatch with JSON; CSV remains supported on the multipart path only).
2. **No `MarkdownToDocument` / `HTMLToDocument`.** Both are too coarse — they flatten structure to plain text and discard fences/headings/list semantics that downstream stages (chunker, LLM) can use.
3. **AST-based chunkers** for Markdown and HTML: parse to a token tree, chunk on **structural boundaries** (headings, paragraphs, fenced code blocks, list items, `<article>`/`<section>`/`<pre>` elements), emit **two text views per chunk**.
4. **Dual-field ES schema:**
   - `content` ← *normalized* text (what gets embedded by bge-m3 + BM25-analyzed).
   - `raw_content` ← *original* chunk slice (what gets returned to LLM context and to chat citations).

This supersedes the Round 2 plan (`docs/team/2026_05_06_mime_aware_converters.md`). The single-converter shortcut is replaced with structure-aware chunkers; the embed-clean-return-raw pattern is adopted.

**Triggered rules / context**
- Round 2 retrieval-fidelity discussion identified that `MarkdownToDocument` strips fences and `_CharBudgetChunker` then space-flattens code into prose. AST chunking + raw retention fixes both at once.
- HTML noise (`<script>`, `<nav>`, `<style>`) must still be excluded from the embedding space — this is now the chunker's job, not a converter's.
- TIDY-FIRST split is **mandatory**: ES mapping change is structural; chunker swap is behavioral; chat-pipeline read-path update is behavioral.

### Role Perspectives (One Line Each)

- 🏗 **Architect**:
  [Pro] Structure-aware chunking is the canonical RAG pattern for documents with semantics (LangChain `MarkdownHeaderTextSplitter`, LlamaIndex `MarkdownNodeParser`, Unstructured.io `partition_html`). Putting it inside our pipeline is the **right** layer; converters were the wrong abstraction.
  [Con] Two custom Haystack components (`_MarkdownASTChunker`, `_HTMLASTChunker`) replace one stock converter — net +~200 LOC. Mitigated by surgical scope and tests.

- ✅ **QA**:
  [Pro] Easy-to-pin contract: chunk boundaries align with structural elements; no chunk ever splits inside a fenced code block; `raw_content` round-trips byte-for-byte through to citations.
  [Con] AST chunkers must be deterministic — same input → same chunks (idempotency requirement R4/S25 still applies). Add property tests for stability.

- 🛡 **SRE**:
  [Pro] No new external service. `markdown-it-py` (~200 KB) and `selectolax` or `lxml` (already pulled by trafilatura — but we now drop trafilatura) — net dep cost is **smaller** than Round 2 (no `trafilatura`, no `boilerpy3`).
  [Con] ES storage roughly **+50–80%** per chunk because `raw_content` duplicates `content` for plain text and adds markup for MD/HTML. Acceptable for our corpus size; document the projection in §4.6.

- 🔍 **Reviewer**:
  [Pro] Resolves both Round-2 dissents (HTML extractor choice, code-fence loss) by removing the converter layer entirely. Single, explicit chunker per MIME with deterministic structural rules.
  [Con] `raw_content` must be excluded from BM25 / embedding to avoid double-counting noise. ES mapping must mark it `index: false` or use `enabled: false` on the field — easy to get wrong; pin in mapping test.

- 📋 **PM**:
  [Pro] User-visible win: chat citations show original markdown/HTML structure (fenced code blocks, headings, lists) not flattened prose. This is the actual product quality bar.
  [Con] Migration: existing chunks have no `raw_content`. Decision: **read-path tolerates null** and falls back to `content`. Supersede converges naturally over time.

- 💻 **Dev**:
  [Pro] Markdown AST via `markdown-it-py` token stream is well-trodden; HTML AST via `selectolax` is fast and small. Both have reference implementations to crib from. No new asyncio surface.
  [Con] HTML "boilerplate" detection (drop `<nav>`, `<aside>`, `<footer>`, `<script>`, `<style>`) needs an explicit allow-list of structural tags rather than heuristics — keep it dumb and predictable for v1.

### Voting Results

| Item | Architect | QA | SRE | Reviewer | PM | Dev | Result |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Inline endpoint scoped to `{text/plain, text/markdown, text/html}` (CSV excluded from inline) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Drop `MarkdownToDocument` and `HTMLToDocument` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Custom AST chunkers: `_MarkdownASTChunker` (markdown-it-py) and `_HTMLASTChunker` (selectolax) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Dual-field ES: `content` (embedded+BM25) ← normalized; `raw_content` (stored only) ← original | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Chat read-path returns `raw_content` to LLM and citations; falls back to `content` if null | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |
| Migration via supersede; no backfill tooling | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **Pass 6/6** |

**Overall: PASS 6/6.** Convergence locked.

### Decision Summary

#### 1. MIME scope (final)

| MIME | Multipart `/ingest` | Inline `/ingest/inline` | Pipeline branch |
|---|:-:|:-:|---|
| `text/plain` | ✅ | ✅ | text-pass-through |
| `text/markdown` | ✅ | ✅ | **markdown AST chunker** |
| `text/html` | ✅ | ✅ | **html AST chunker** |
| `text/csv` | ✅ | ❌ (415) | text-pass-through, line-mode chunker (unchanged) |

Two enums (single source of truth):
```python
class IngestMime(StrEnum):       # multipart accepts these
    TEXT_PLAIN    = "text/plain"
    TEXT_MARKDOWN = "text/markdown"
    TEXT_HTML     = "text/html"
    TEXT_CSV      = "text/csv"

class InlineIngestMime(StrEnum): # inline rejects CSV
    TEXT_PLAIN    = IngestMime.TEXT_PLAIN.value
    TEXT_MARKDOWN = IngestMime.TEXT_MARKDOWN.value
    TEXT_HTML     = IngestMime.TEXT_HTML.value
```

#### 2. Pipeline graph (replaces Round 2 design)

```
ByteStream(meta.mime_type)
        │
        ▼
   FileTypeRouter
        │
        ├── text/plain    ──► TextFileToDocument ──┐
        ├── text/csv      ──► TextFileToDocument ──┤  (no AST chunking; existing line-mode path)
        ├── text/markdown ──► _MarkdownASTChunker ─┤
        ├── text/html     ──► _HTMLASTChunker ─────┤
        └── unclassified  ──► _RaiseUnroutable     │
                                                   ▼
                                          DocumentJoiner
                                                   │
                                                   ▼
                                  _IdempotencyClean (R4/S25, unchanged)
                                                   │
                                                   ▼
                            _CharBudgetChunker (only for plain/csv branches;
                            MD/HTML chunks already structural and bypass it)
                                                   │
                                                   ▼
                                    DocumentEmbedder (embeds .content)
                                                   │
                                                   ▼
                                    DocumentWriter → ES chunks_v1
```

**Note on `DocumentCleaner`**: removed. AST chunkers emit final-form chunks; plain/csv path doesn't need its tag-stripping. Saves a component.

#### 3. AST chunker contract

Each chunker produces `list[Document]` where every Document carries:

| Field | Source | Purpose |
|---|---|---|
| `Document.content` | normalized text (markup stripped, whitespace collapsed) | embedded by bge-m3, BM25-analyzed |
| `Document.meta["raw_content"]` | exact byte-slice of the original input | persisted to ES `raw_content`, returned to LLM and citations |
| `Document.meta["document_id"]` | from worker | join key |
| `Document.meta["split_id"]` | sequential | ordering |
| `Document.meta["content_type"]` | branch MIME | preserved for chunker profile compatibility |

**Markdown chunker rules (`_MarkdownASTChunker`):**
- Parse with `markdown-it-py` to token stream.
- **Atomic units** (never split): heading + following content up to next same-or-higher-level heading; full fenced code block; full list; full table; full blockquote.
- **Greedy pack** atomic units into chunks ≤ `CHUNK_TARGET_CHARS_EN` (2000); units exceeding target are emitted standalone.
- `Document.content` = `markdown-it-py` plain-text rendering of the unit (fence delimiters dropped, structure flattened, but headings retained as text).
- `Document.meta["raw_content"]` = raw markdown byte-slice spanning the unit (fences, headings, lists intact).

**HTML chunker rules (`_HTMLASTChunker`):**
- Parse with `selectolax` (lexbor backend).
- **Drop nodes**: `<script>`, `<style>`, `<nav>`, `<aside>`, `<footer>`, `<header>` (when not inside `<article>`/`<main>`), `<noscript>`, comment nodes.
- **Atomic units**: each `<h1>`–`<h6>`-led section (headings cluster with following siblings until next same-or-higher heading); each `<pre>`/`<code>` block; each `<table>`; top-level `<article>`/`<main>` paragraphs.
- `Document.content` = `.text()` of the unit (text nodes joined with structural whitespace).
- `Document.meta["raw_content"]` = serialized outer HTML of the unit (preserves tags for LLM/citations).

Both chunkers must be **byte-stable**: same input → identical chunk boundaries (R4/S25 idempotency). Add property tests asserting this.

#### 4. ES `chunks_v1` mapping change

| Field | Old | New |
|---|---|---|
| `content` | `text` analyzer-EN, BM25-indexed; embedded by bge-m3 | unchanged (semantics: now stores **normalized** text) |
| `raw_content` | — | `text`, `index: false`, `doc_values: false`, kept in `_source` only |
| `embedding` | dense_vector 1024 | unchanged |
| `document_id`, `source_id`, `source_app`, `source_workspace`, `title`, `split_id`, `split_idx_start` | | unchanged |

`raw_content` adds storage but **zero indexing cost** and **zero retrieval-time cost** — only fetched on hit via `_source`.

Mapping change is **backward-compatible** (additive field, nullable). New chunks populate it; old chunks have it absent.

#### 5. Chat read-path update

In `src/ragent/pipelines/chat.py`:
- LLM-context construction reads `doc.meta.get("raw_content") or doc.content`.
- Citation excerpts (`_ExcerptTruncator`) operate on the same fallback.
- Reranker continues to score on `content` (normalized) — keeps reranking quality consistent with retrieval scoring.

This is the **only** chat-side change. No API contract change; `sources[].excerpt` already exists and just gets richer content.

#### 6. Pyproject deltas (vs. Round 2 plan)

- Add: `markdown-it-py>=3,<4`, `selectolax>=0.3,<0.4`.
- **Do not add**: `trafilatura`, `boilerpy3` (Round 2 plan's HTML extractors — superseded).
- Net: smaller dep footprint than Round 2.

### TDD Plan (replaces Round 2 tasks T3.20–T3.26)

| ID | Phase | Achieve / Deliver | Depends | Status | Owner |
|---|---|---|---|---|---|
| T3.30 | Structural | ES mapping migration: add `raw_content` (`type: text, index: false, doc_values: false`) to `chunks_v1`. Schema-drift test asserts presence; existing chunks tolerate null. **Commit `[STRUCTURAL]`.** | T0.8d | [ ] | Dev |
| T3.31 | Structural | Extract today's converter wiring (`factory.py:248`) into `_build_branch(...)` helper, behavior unchanged. **Commit `[STRUCTURAL]`.** | T3.30 | [ ] | Dev |
| T3.32 | Red | `tests/unit/test_markdown_ast_chunker.py` — atomic units (heading-section, fenced code, list, table) never split; `meta["raw_content"]` round-trips byte-for-byte; `content` is plain-text rendering; deterministic across re-runs (property test). | T3.31 | [ ] | QA |
| T3.33 | Green | Implement `_MarkdownASTChunker` (`pipelines/factory.py`). | T3.32 | [ ] | Dev |
| T3.34 | Red | `tests/unit/test_html_ast_chunker.py` — `<script>/<style>/<nav>/<aside>/<footer>` excluded from both `content` and `raw_content`; heading-led sections atomic; `<pre>` atomic; `meta["raw_content"]` is well-formed HTML fragment; deterministic. | T3.31 | [ ] | QA |
| T3.35 | Green | Implement `_HTMLASTChunker` (`pipelines/factory.py`). | T3.34 | [ ] | Dev |
| T3.36 | Red | `tests/unit/test_ingest_pipeline_routing.py` — `FileTypeRouter` routes each MIME to the right branch; unclassified raises `UnroutableMimeError`; MD/HTML chunks bypass `_CharBudgetChunker`. | T3.33, T3.35 | [ ] | QA |
| T3.37 | Green | Wire `FileTypeRouter` + AST chunkers + `_RaiseUnroutable` into `build_ingest_pipeline`. **Commit `[BEHAVIORAL]`.** | T3.36 | [ ] | Dev |
| T3.38 | Red | `tests/unit/test_chunk_repository_raw_content.py` — write path persists `raw_content`; read path returns it; null tolerated for legacy rows. | T3.30 | [ ] | QA |
| T3.39 | Green | `repositories/document_repository.py` (or chunk repo equivalent): persist + retrieve `raw_content`. | T3.38 | [ ] | Dev |
| T3.40 | Red | `tests/unit/test_chat_uses_raw_content.py` — LLM-context builder and citation excerpts prefer `raw_content` when present, fall back to `content` when null. Reranker still scores on `content`. | T3.39 | [ ] | QA |
| T3.41 | Green | Update `pipelines/chat.py` LLM-context and `_ExcerptTruncator`. **Commit `[BEHAVIORAL]`.** | T3.40 | [ ] | Dev |
| T3.42 | Red | `tests/integration/test_pipeline_unroutable.py` — stale MinIO object with disallowed MIME → worker FAILED + `error_code=PIPELINE_UNROUTABLE`. | T3.37 | [ ] | QA |
| T3.43 | Green | Worker maps `UnroutableMimeError` → FAILED + cleanup (mirrors `PIPELINE_TIMEOUT` path). | T3.42 | [ ] | Dev |
| T3.44 | Refactor | `pyproject.toml`: add `markdown-it-py`, `selectolax`. Lockfile refresh. `make check` green. | T3.37 | [ ] | Dev |
| T3.45 | Acceptance | `tests/e2e/test_ingest_markdown_html_fidelity.py` — POST a markdown doc with fenced code → READY → `/chat` returns answer that contains the code as a fenced block with original language tag (LLM reconstructs from `raw_content`). Same for HTML doc with `<pre><code>`. Boilerplate (`<nav>`, `<script>`) absent from both `content` and `raw_content`. | T3.41, T3.43, T7.2 | [ ] | QA |

### Spec / Journal updates

**`docs/00_spec.md`:**
- §3.2 pipeline graph: replace single-converter diagram with FileTypeRouter + AST chunker design above.
- §5.1 ES schema: add `raw_content` column with `index: false, doc_values: false` rationale.
- §3.4 chat: add note that LLM context and citation excerpts prefer `raw_content`.
- §4.1 routes: lock inline endpoint MIME scope to `{text/plain, text/markdown, text/html}`.

**`docs/00_journal.md` (DOMAIN: pipeline):**
- "Converters that flatten document structure (`MarkdownToDocument`, `HTMLToDocument`) destroy signals the LLM and chunker can use. Prefer AST-aware chunkers that keep structural atomicity (no split inside a fenced code block, a `<pre>`, or a list)."
- "Embed clean, return raw: `content` (normalized) gets vector + BM25; `raw_content` (original byte slice) gets stored in `_source` and shipped to the LLM. Fence delimiters, language tags, headings, and tags survive end-to-end this way."

### Pending Items

- **Deferred Decision — backfill `raw_content` for legacy chunks.** Trigger: corpus age analysis shows > 30% of chunks predate this round and citation quality on those documents is materially worse. Owner: SRE.
- **Deferred Decision — extend AST chunking to PDF/PPTX/DOCX.** Out of scope for this round (transport is multipart binary, separate effort). Trigger: binary inline ingest enters scope. Owner: Architect.
- **Spike — measure ES `_source` storage growth on a 10k-doc sample** before/after `raw_content` rollout. Owner: SRE. Trigger: pre-prod gate.
- **Open question — preserve markdown frontmatter (`---`-delimited YAML)?** AST treats it as text; if it pollutes `content`, strip in normalize step. Decide during T3.32 Red.
