# 00_journal.md — Blameless Team Reflection

> **Goal:** Prevent recurrence through actionable, domain-specific guidelines rather than individual blame.
> Format: `docs/rule.md` §00_journal table.

| Date | Domain | Issue Description | Root Cause | Actionable Guideline (Prevention) |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-03 | **Architecture** | Risk of premature abstraction in `ExtractorPlugin` Protocol if shaped around hypothetical Phase 3 graph needs. | Designing for unverified future requirements violates YAGNI (`CLAUDE.md` §Simplicity First). | **[Rule]** Plugin Protocol fields must be justified by an existing in-Phase consumer. New fields require a failing test in the same commit. |
| 2026-05-03 | **QA** | Phase 1 exit metric "golden 50-Q top-3 ≥ 70%" cannot be measured in W1–W2. | Acceptance metric tied to an asset (golden set) not yet curated. | **[Rule]** Every exit metric in `00_plan.md` must list its measurement asset (test file, dataset path) and the week it becomes available. Block plan freeze if missing. |
| 2026-05-03 | **Process** | TDD workflow in `CLAUDE.md` mandates structural/behavioral commit separation; mixing them silently breaks bisect. | Convenience temptation when a refactor surfaces during a feature commit. | **[Rule]** Commit messages must start with `[STRUCTURAL]` or `[BEHAVIORAL]`. Pre-push hook (Phase 2) will reject mixed commits. |
