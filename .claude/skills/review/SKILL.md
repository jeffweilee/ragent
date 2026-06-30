# Review: Plan and Spec Compliance Check

Perform a review of the current staged changes covering plan compliance, spec alignment, test coverage, and code quality.

## Mode selection

This skill accepts an optional `--mode fast|full` argument (default: `full`).

| Mode | When to use | What runs |
|------|-------------|-----------|
| `--mode fast` | Pre-commit fast gate on low-risk or high-risk staged diffs | Focused single-pass check of the four compliance dimensions |
| `--mode full` | Pre-push full gate on high-risk commits; default when no mode given | Three parallel sub-agents (Plan & Spec · Domain Boundaries · Tests & Quality) |

**Stamp used:** `review:fast` or `review:full` (see Stamp step).

---

## fast mode

1. Run `git diff --cached` to get the staged diff.
2. In a single response, check all four dimensions:
   - **Plan compliance**: do the staged changes complete the next unmarked `[ ]` item in `docs/00_plan.md`? Any obvious gaps?
   - **Spec alignment**: do HTTP shapes, error codes, DB schema, env-var names match `docs/00_spec.md`? Spot-check the most relevant sections for the diff.
   - **Test coverage**: every new code path has a test; no new unreachable branches.
   - **Code quality**: no obvious duplication, no dead code, no commented-out code.
3. Report LGTM or list findings. Fix any that are clear-cut and re-stage.

Then stamp:

```bash
RAGENT_SKILL_INVOCATION_TOKEN=1 bash .claude/hooks/stamp_pre_commit_approved.sh review:fast
```

---

## full mode (default)

> **MANDATORY — no exceptions:** ALWAYS launch all three sub-agents below even if the diff appears small, focused, or surgical. The phrase *"diff is small/focused/inline review sufficient"* is a process violation — see journal Process 2026-05-17 "Inline /simplify rationalization". The fan-out **is** the review; skipping it means skipping the review.

1. Run `git diff --cached` (or `git diff origin/<branch>..HEAD` if triggered from the pre-push gate for a high-risk commit) to get the diff.
2. Use the Agent tool to launch all three agents below concurrently in a single message. Pass each agent the full diff plus the doc(s) named in its section — each agent reads only what its dimension needs, not all three docs.

### Agent 1: Plan & Spec Compliance

- Read the relevant sections of `docs/00_plan.md` for the items being committed. Verify every objective in `00_plan.md` for this cycle is fully implemented — no partial or skipped items.
- Read the relevant sections of `docs/00_spec.md`. Verify behaviour matches its contracts (HTTP shapes, error codes, streaming framing, DB schema, etc.).

### Agent 2: Domain Boundaries

- Read `docs/00_domain_map.md §三` (full table: `docs/spec/dependency_rules.md`). Verify no import crosses a forbidden boundary. Common violations to grep for: `pipelines/` importing `repositories/`; any router handler using `Header` with a user-id alias instead of `Depends(get_user_id)` — grep with `grep -rn 'Header.*alias.*[Xx]-[Uu]ser' src/ragent/routers/` (catches both quote styles and case variants); `os.environ` read outside `utility/env.py` or `bootstrap/composition.py`; `services/` importing `routers/`.

### Agent 3: Test Coverage & Code Quality

- Verify every new behaviour path has a corresponding test; no dead or unreachable code.
- Verify no duplication, no hidden coupling, no premature abstraction, no commented-out code.
- Mock return values must be real instances of the mocked type, not a `dict` or bare `MagicMock()` (`00_rule.md` §Test Log Capture).
- New `build_container()` constructor branches/kwargs must have a test calling them with the exact kwargs `composition.py` passes in production (`00_rule.md` §Composition Root: Production-Wiring Coverage).

3. Wait for all three agents to complete. Aggregate their findings; if any require fixes, make them and re-stage.
4. Report LGTM or list findings.

Then stamp:

```bash
RAGENT_SKILL_INVOCATION_TOKEN=1 bash .claude/hooks/stamp_pre_commit_approved.sh review:full
```
