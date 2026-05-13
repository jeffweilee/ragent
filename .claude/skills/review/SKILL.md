# Review: Plan and Spec Compliance Check

Perform a thorough review of the current staged changes covering **all** of the following; address every finding before proceeding:

- **Plan compliance**: every objective in `docs/00_plan.md` for this cycle is fully implemented — no partial or skipped items.
- **Spec alignment**: behaviour matches `docs/00_spec.md` contracts (HTTP shapes, error codes, streaming framing, DB schema, etc.).
- **Test coverage**: every new behaviour path has a corresponding test; no dead or unreachable code.
- **Code quality**: no duplication, no hidden coupling, no premature abstraction, no commented-out code.

## Steps

1. Run `git diff --cached` to get the staged diff.
2. Read the relevant sections of `docs/00_plan.md` and `docs/00_spec.md` for the items being committed.
3. Analyze the changes and provide a thorough review covering plan compliance, spec alignment, test coverage, and code quality.
4. If findings require fixes, make them and re-stage.
5. Report LGTM or list findings.

## Stamp (mandatory final step)

After completing the review and confirming LGTM (or after all findings are resolved), run:

```bash
RAGENT_SKILL_INVOCATION_TOKEN=1 bash .claude/hooks/stamp_pre_commit_approved.sh review
```
