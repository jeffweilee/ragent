# 00_journal.md — Blameless Team Reflection

> **Goal:** Prevent recurrence through actionable, domain-specific guidelines rather than individual blame.
> Format: `docs/00_rule.md` §00_journal table.
> Deduplication: each rule applies to a distinct trigger; merging happens when triggers fully overlap.

| Date | Domain | Issue Description | Root Cause | Actionable Guideline (Prevention) |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-03 | **Architecture** | Risk of premature abstraction in `ExtractorPlugin` Protocol if shaped around hypothetical Phase 3 graph needs. | Designing for unverified future requirements violates YAGNI. | **[Rule]** Plugin Protocol fields must be justified by an existing in-Phase consumer. New fields require a failing test in the same commit. |
| 2026-05-03 | **QA** | Phase 1 exit metric "golden 50-Q top-3 ≥ 70%" cannot be measured in W1–W2. | Acceptance metric tied to an asset (golden set) not yet curated. | **[Rule]** Every exit metric in `00_plan.md` must list its measurement asset (test file, dataset path) and the week it becomes available. Block plan freeze if missing. |
| 2026-05-03 | **Process** | TDD workflow mandates structural/behavioral commit separation; mixing them silently breaks bisect. | Convenience temptation when a refactor surfaces during a feature commit. | **[Rule]** Commit messages must start with `[STRUCTURAL]` or `[BEHAVIORAL]`. Pre-push hook (Phase 2) will reject mixed commits. |
| 2026-05-04 | **PM/Spec** | Plan W3 jumped to "Ingest Pipeline" without CRUD lifecycle TDD; spec lifecycle existed but was not decomposed into tasks. | Plan author did not enumerate every BDD scenario as a TDD `[ ]` row. | **[Rule]** Every BDD scenario in `00_spec.md` §5 must be backed by ≥1 row in `00_plan.md` whose test path matches the scenario name. Verified at end of each round. |
| 2026-05-04 | **Architecture** | Spec §7.1 contained DB FK + JSON ACL column, in conflict with `00_rule.md` (no physical FK; OpenFGA is ACL system of record). | Spec was authored before reading the latest `00_rule.md`. | **[Rule]** Whenever `00_rule.md` is updated, the next agent-team round must re-validate spec/plan against the new rule version and cite it in the team output. |
| 2026-05-04 | **Security** | TokenManager refresh boundary (`expiresAt − 5 min`) is silent failure if missed → all third-party calls fail simultaneously when token expires. | Boundary timing not initially in BDD scenarios. | **[Rule]** Every external dependency with token TTL must have a "boundary refresh" Red test using a fake clock. |
| 2026-05-04 | **Architecture** | Concurrent worker + Reconciler can both update `documents.status`/`attempt`, causing double-dispatch. | No explicit locking on state-machine mutations. | **[Rule]** All `documents.status` mutations use `SELECT … FOR UPDATE` (Worker) or `FOR UPDATE SKIP LOCKED` (Reconciler). Repository.`update_status` raises `IllegalStateTransition`. |
