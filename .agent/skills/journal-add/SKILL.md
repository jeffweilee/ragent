---
name: journal-add
description: Append a blameless reflection row to docs/00_journal.md under one of the six fixed domains (Architecture, SRE, QA, Security, Spec, Process). Use after a test failure has been root-caused and fixed, or whenever .claude/pending_journal.md is non-empty. Validates the row schema before write so 00_rule.md format is preserved.
---

# /journal-add — Close the Error-Learning Loop

Append a row to `docs/00_journal.md` so the next session learns from this mistake.

## When to invoke
- Immediately after a green fix that resolved a non-trivial failure.
- When `.claude/pending_journal.md` is non-empty (the Stop hook will block until it is empty).
- When `00_rule.md` is updated and a related lesson must be recorded.

## Required fields (schema)

| Field | Constraint |
| :--- | :--- |
| `Domain` | MUST be one of: `Architecture`, `SRE`, `QA`, `Security`, `Spec`, `Process`. Do **not** invent new domains. |
| `Date` | Today's date, ISO `YYYY-MM-DD`. |
| `Topic` | 1–3 words, scannable tag (e.g. `Connection Pool`, `OTEL Fork`, `Tidy First`). |
| `Description` | One sentence — what happened, observable symptom. |
| `Root Cause` | One sentence — the underlying reason, not blame. |
| `Actionable Guideline` | One sentence starting with `**[Rule]**`, imperative voice, testable. |

## Procedure

1. Read `.claude/pending_journal.md` (if present) to recover the failing test ID and error line.
2. For each pending entry, ask the user (or decide from context) which domain it belongs to. If unsure between two domains, pick the one whose existing table already covers the closest topic — never create a new domain.
3. Confirm the row with the user before write when ambiguity exists.
4. Append the row to the matching `## <Domain>` table in `docs/00_journal.md`, preserving the existing 5-column markdown format:

   ```
   | YYYY-MM-DD | <Topic> | <Description> | <Root Cause> | **[Rule]** <Actionable Guideline> |
   ```

5. Validate after write:
   - `grep -c '^| 20' docs/00_journal.md` increased by exactly the number of rows added.
   - The new row appears under exactly one of the six allowed `## ` headings.
   - No new `## ` heading was introduced.
6. Truncate `.claude/pending_journal.md` (`: > .claude/pending_journal.md`) once all entries are filed.
7. Stage `docs/00_journal.md` and include it in the next `[STRUCTURAL]` commit (journal updates are documentation, never mixed with behavioral diffs).

## Anti-patterns (reject)

- Adding a row under a brand-new domain heading.
- Multi-sentence `Description` or `Root Cause` (forces the reader to scroll; defeats scannability).
- Guidelines phrased as wishes ("we should try to…") rather than rules ("**[Rule]** All X must Y.").
- Filing the same lesson under two domains — pick one.
- Committing journal rows together with production code changes (violates Tidy First).
- **Reusing a Topic already present in the domain's table.** Before writing the topic tag, `grep` the existing rows in that domain for the proposed word(s). If the same tag exists, choose a more specific 1–3-word alternative (e.g., if "Resilience" already appears, use "Cleanup Ordering", "Lock Scope", or "Heartbeat Liveness" — whatever distinguishes the new entry). Duplicate topics break the table's primary purpose: letting a reader locate a specific entry by scanning topics alone.

## Pre-write validation checklist

Before appending, answer each question:

1. **Domain**: does it map to one of the six fixed headings without ambiguity?
2. **Topic uniqueness**: does `grep -i "<topic>" docs/00_journal.md` return any row in the same domain? If yes, pick a different tag.
3. **Topic length**: is the tag 1–3 words? If longer, abbreviate.
4. **Guideline form**: does the `Actionable Guideline` start with `**[Rule]**` and use imperative voice?
5. **Row count delta**: does `grep -c '^| 20' docs/00_journal.md` increase by exactly the number of rows added?

## Example

Pending entry:
```
2026-05-07 test_chat_stream_propagates_otel_context FAILED: Failed to detach context
```

Filed row (under `## Architecture`):
```
| 2026-05-07 | OTEL Async Boundary | Span context lost across `asyncio.create_task` in chat stream. | `trace.use_span()` was used across an async-context boundary. | **[Rule]** Async tasks must capture `parent_ctx = trace.set_span_in_context(span)` and pass `context=parent_ctx` to `start_as_current_span`. |
```
