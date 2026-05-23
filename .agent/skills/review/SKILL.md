# Review: Plan and Spec Compliance Check

Perform a review of the current staged changes covering plan compliance, spec alignment, test coverage, and code quality.

## Mode selection

This skill accepts an optional `--mode fast|full` argument (default: `full`).

| Mode | When to use | What runs |
|------|-------------|-----------|
| `--mode fast` | Pre-commit fast gate on low-risk or high-risk staged diffs | Focused single-pass check of the four compliance dimensions |
| `--mode full` | Pre-push full gate on high-risk commits; default when no mode given | Full multi-step review with doc reads and fix loop |

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

1. Run `git diff --cached` (or `git diff origin/<branch>..HEAD` if triggered from the pre-push gate for a high-risk commit) to get the diff.
2. Read the relevant sections of `docs/00_plan.md` and `docs/00_spec.md` for the items being committed.
3. Analyze the changes and provide a thorough review covering:
   - **Plan compliance**: every objective in `docs/00_plan.md` for this cycle is fully implemented — no partial or skipped items.
   - **Spec alignment**: behaviour matches `docs/00_spec.md` contracts (HTTP shapes, error codes, streaming framing, DB schema, etc.).
   - **Test coverage**: every new behaviour path has a corresponding test; no dead or unreachable code.
   - **Code quality**: no duplication, no hidden coupling, no premature abstraction, no commented-out code.
4. If findings require fixes, make them and re-stage.
5. Report LGTM or list findings.

Then stamp:

```bash
RAGENT_SKILL_INVOCATION_TOKEN=1 bash .claude/hooks/stamp_pre_commit_approved.sh review:full
```
