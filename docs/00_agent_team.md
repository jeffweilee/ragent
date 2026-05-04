# RAGENT Agent Team

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
5. **Output**: Decision summary + Trade-offs OR Pending items for next round. Update `docs/00_plan.md`, `docs/00_spec.md`.
6. **Track Completion Gate** *(mandatory before claiming any track done)*:
   - **Print to console** the full task table for the track being closed — every row with its current `[x]`/`[ ]`/`[~]` status.
   - **Audit each `[x]` honestly**: confirm the backing test (a) exists, (b) actually **ran** in the current environment (not skipped, not mocked-away infrastructure), and (c) **passed**. A skipped test is `[ ]`, not `[x]`.
   - **Zero `[ ]` required**: if any task remains `[ ]` (including Docker-gated tests that have never run green), the track is NOT done. State so explicitly.
   - **Update `docs/00_plan.md`** to the verified state — revert any prematurely-set `[x]` back to `[ ]`.
   - **Example console output** for T0:
     ```
     Track T0 — Completion Audit
     T0.1  [x] Scaffold           — unit tests pass
     T0.2  [x] Makefile/CI alias  — make check green
     T0.8b [ ] Schema drift test  — SKIPPED (Docker not available, test never ran)
     T0.8c [ ] Bootstrap auto-init— SKIPPED (Docker not available, test never ran)
     ⚠  Track T0 NOT complete: 2 tasks remain [ ]
     ```
7. **Reflection**: Refine `docs/00_journal.md`, prevent error-prone recurrence through actionable, domain-specific guidelines. Deduplicate required.
8. **Next**: Enter next round until plan and spec and implementation matches and no todo items.

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
| **Reviewer** | Yes | Strict auditing of delivered code; ensuring clean, concise, high-performance, high-quality, readible code which match spec and plan with adequate test. | |
| **Fullstack Senior Developer** | Yes | Proficiency in the project tech stack; honest delivery of high-quality code with completed test, lint, and format before submission. | |
