# Rule

- **Always** Check and Update following documents Before and After planning and delivery.
   - `docs/00_spec.md`: Specification Standards
   - `docs/00_plan.md`: Master TDD Implementation Checklist
   - `docs/00_journal.md` (Blameless Team Reflection)
- **Always** execute commands before commit
- **Always** use "Agent Team Member" and follow workflow.


### `docs/00_spec.md`: Specification Standards

| Section | Inclusion | Exclusion |
| :--- | :--- | :--- |
| **Mission & Objective** | System or module goals (**WHAT**) | Implementation methods, detailed steps (**HOW**) |
| **Domain Boundary** | System scope and inter-module relationships. Fields: Domain Topic, Responsibilities, Out-of-Scope | Functional requirement lists |
| **Business Process** | High-level business flows: Happy path, error handling. Use simple wireframe flowcharts (readable in 1s). | Granular logic branch flows, specific edge-case business scenarios |
| **Business Scenario** | Low-level business details. Use simple Mermaid flowcharts or sequence diagrams (readable in 1s). | Data models, interface definitions |
| **Scenario Testing** | Behavior-Driven Development (TDD/BDD). Fields: Domain, Scenario, Given, When, Then | Actual implementation code |
| **System Interface** | (Optional) API endpoints, Interface definitions, and samples | Internal implementation class or object naming details |
| **Data Structure** | (Optional) Database schemas/fields, Elasticsearch Index settings, and mappings | Internal implementation class or object or Data models or naming details |


### `docs/00_plan.md`: Master TDD Implementation Checklist

| Phase | Category | Task | Status | Owner |
| :--- | :--- | :--- | :---: | :--- |
| **Phase 1** | **Analysis** | Define Domain Boundaries and Mission Objectives in `spec.md`. | [ ] | Architect |
| **Phase 1** | **Design** | Map Business Scenarios & write Given-When-Then test cases. | [ ] | QA / PM |
| **Phase 1** | **Red** | Write and execute failing tests to define behavior expectations. | [ ] | QA / Dev |
| **Phase 1** | **Green** | Implement minimal production code to pass all unit/integration tests. | [ ] | Dev |
| **Phase 1** | **Refactor** | Code Review: Enforce Clean Code, Idempotency, and Performance. | [ ] | Reviewer |
| **Phase 2** | **Stability** | SRE check: HA verification, Monitoring setup, and Alerting rules. | [ ] | SRE |
| **Phase 2** | **Closure** | Sync all document updates and record lessons in `00_journal.md`. | [ ] | Master |


### `docs/00_journal.md` (Blameless Team Reflection)

> **Goal:** Prevent recurrence through actionable, domain-specific guidelines rather than individual blame.

| Date | Domain | Issue Description | Root Cause | Actionable Guideline (Prevention) |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-04 | **Architecture** | Race condition during high-concurrency wallet updates. | Missing atomicity at the DB transaction level. | **[Rule]** All balance-related mutations must use Pessimistic Locking and be wrapped in an atomic decorator. |
| 2026-05-01 | **SRE** | Production downtime during schema migration. | Migration script lacked backward compatibility with the current API. | **[Rule]** Use "Expand and Contract" pattern for DB changes; never drop a column before the code reference is removed. |
| 2026-04-25 | **QA** | Missed edge case in discount logic. | TDD Given-When-Then did not account for "Expired" status. | **[Rule]** All state-machine transitions must include a "Negative Path" test case in the Scenario Matrix. |

---
# Command
**Always** run these commands before commit.

## Python
- Format: `uv run ruff format .`
- Lint: `uv run ruff check . --fix`
- Test: `uv run pytest`

---

# Agent Team

## When use Agent Team
- During Planning
- Under Development
- Before Delivery

## When Not to use Agent Team
- Summary
- Simple Question or Fact

## Workflow

1. **Initiation**: Master announces topic and identifies relevant principles.
2. **Debate**: 6 roles present **Pro & Con** views (must cite references/evidence).
3. **Conflict Identification**: Master highlights core points of contention.
4. **Voting**: 6 roles vote (Approve/Reject/Abstain).
   - **Pass**: $\ge 4/6$ Approve.
   - **Veto**: $\ge 4/6$ Reject.
   - **Tie (3:3)**: Master casts the deciding vote.
   - **Else**: Move to next round.
5. **Output**: Decision summary + Trade-offs OR Pending items for next round.

---

## Multi-Round & Convergence

- **Max Rounds**: 3. If unresolved after Round 1, auto-start next round focusing on "Pending Items."
- **Prohibited Patterns**: Silent "Con" views, fake consensus without debate, or redundant arguments.
- **Forced Convergence (End of Round 3)**: Unresolved items must be tagged:
  - **Accepted Risk**: Accept risk and proceed (assign owner).
  - **Deferred Decision**: Set trigger condition for future discussion.
  - **Downgrade to Spike**: Small-scale validation to gather data.

---

## Team Output Template (`docs\team\YYYY_MM_DD_{topic}.md`)

```markdown
## Discussion: [Topic]

### Master's Opening
[Triggered rules/context]

### Role Perspectives (One Line Each)
- 🏗 Architect: [Pro] ... [Con] ...
- ✅ QA: [Pro] ... [Con] ...
- 🛡 SRE: [Pro] ... [Con] ...
- 🔍 Reviewer: [Pro] ... [Con] ...
- 📋 PM: [Pro] ... [Con] ...
- 💻 Dev: [Pro] ... [Con] ...

### Voting Results
[Role Votes] | Result: [Pass/Fail/Next Round]

### Decision Summary
[Approved content + Accepted trade-offs/costs]

### Pending Items
[Unresolved issues for next round or Convergence Tags]
```

---

## Agent Team Member
| Role | Voting Member | Responsibilities | Reference Documents |
|:---|:---:|:---|:---|
| **Master** | Tie-breaker only | Moderate debates, drive consensus, and enforce protocol compliance. | |
| **PM** | Yes | Delivery planning, requirements alignment, and scope management. | |
| **Architect** | Yes | DDD Bounded Context, SDD specifications, and architectural design (High Performance, Idempotency, Atomicity). | |
| **QA** | Yes | TDD Red-Green-Refactor, Integration Test Matrix, and test acceptance. | |
| **SRE** | Yes | High Availability (HA), Zero-Downtime deployment, and monitoring/alerting. | |
| **Reviewer** | Yes | Strict auditing of delivered code; ensuring clean, concise, high-performance, and high-quality code. | |
| **Fullstack Senior Developer** | Yes | Proficiency in the project tech stack; honest delivery of high-quality code with completed test, lint, and format before submission. | |
