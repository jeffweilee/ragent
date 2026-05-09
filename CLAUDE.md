# CLAUDE.md - Project Guidelines & TDD Workflow

* Read `docs/00_rule.md` and follow development standards and mandatory workflow for this project. 
* Strict adherence to **TDD (Test-Driven Development)** and **Minimalism** and **Integrity** are required.
---

## Tech Stack
- FastAPI (Python 3.12, `uv` package management)
- TaskIQ + Redis Sentinel
- Haystack 2.x
- MariaDB 10.6
- Elasticsearch 9.2.3
- Neo4j 5.2.6
- Third-Party Customized API
  - Embedding API
  - LLM API
  - Rerank API
- Observability
  - OpenTelemetry
  - Grafana
  - Prometheus 

---

## THE TDD WORKFLOW

Whenever team start to work or user says "go" or "continue", follow these steps precisely:

1.  **Read `plan.md`**: Find the next unmarked test item `[ ]`.
2.  **Red Phase**: Implement the failing test first. Verify it fails.
3.  **Green Phase**: Write the **minimum** code necessary to make the test pass.
4.  **Refactor Phase**: Clean up the code while ensuring tests remain green.
5.  **Verification**: Run test, lint, format.
6.  **Progress**: Mark the test as complete `[x]` in `plan.md`.
7.  **Simplify**: Run `/simplify` — AI code quality pass; stage any resulting fixes and re-verify tests pass.
8.  **Review**: Run `/review` — team code review covering **all** of the following; address every finding before proceeding:
    - **Plan compliance**: every objective in `docs/00_plan.md` for this cycle is fully implemented — no partial or skipped items.
    - **Spec alignment**: behaviour matches `docs/00_spec.md` contracts (HTTP shapes, error codes, streaming framing, DB schema, etc.).
    - **Test coverage**: every new behaviour path has a corresponding test; no dead or unreachable code.
    - **Code quality**: no duplication, no hidden coupling, no premature abstraction, no commented-out code.

    After both 7 and 8 pass with no outstanding issues, stamp the approval marker:
    ```bash
    bash .claude/hooks/stamp_approval.sh
    ```
9.  **Commit**: Git commit with `[BEHAVIORAL]` or `[STRUCTURAL]` prefix.
    _(The pre-commit gate will verify the marker exists and consume it.)_
10. **Documentation**: Follow "RESOURCES" Section to update each document accordingly.
11. **Next**: Start new round and repeat the workflow until all plans matches successful criteria.

---


## TIDY FIRST APPROACH

Separate all changes into two distinct types:

### 1. STRUCTURAL CHANGES

Rearranging code without changing behavior:

- Renaming variables, methods, or classes for clarity
- Extracting methods or functions
- Moving code to more appropriate locations
- Reorganizing imports or dependencies
- Reformatting code

### 2. BEHAVIORAL CHANGES

Adding or modifying actual functionality:

- Implementing new features
- Fixing bugs that change program behavior
- Modifying algorithms or logic
- Adding new dependencies that change behavior

### Critical Rules:

- **Never mix structural and behavioral changes in the same commit**
- **Always make structural changes first** when both are needed
- Validate structural changes do not alter behavior by running tests before and after
- If a structural change breaks tests, revert and investigate

---

## CORE DEVELOPMENT PHILOSOPHY

### 1. Think Before Coding
*   **Surface Trade-offs**: If multiple solutions exist, present them. Never pick silently.
*   **No Assumptions**: Explicitly state assumptions. If a requirement is vague, **stop and ask**.
*   **Push Back**: If a simpler approach exists, suggest it. Avoid over-engineering.

### 2. Simplicity First (YAGNI)
*   **Minimum Viable Code**: No speculative features or "future-proofing."
*   **No Abstractions**: Avoid abstractions for single-use logic.
*   **Refinement**: If 50 lines can do what 200 lines do, rewrite it.

### 3. Surgical Changes
*   **Scope Control**: Touch only what is necessary. Do not "improve" adjacent code or formatting.
*   **Style Matching**: Match existing patterns and idioms, even if you prefer others.
*   **Orphan Cleanup**: Only remove imports/variables made unused by **your** changes.

---

## CODE QUALITY STANDARDS
IMPORTANT: Quality is non-negotiable. Every line of code must be traceable to a test and a specific requirement.

*   **DRY (Don't Repeat Yourself)**: Eliminate duplication ruthlessly.
*   **SRP (Single Responsibility)**: Keep methods small and focused.
*   **Explicit Dependencies**: No hidden coupling or global state side effects.
*   **Documentation**: Explain *why* something is done, not *what* (the code should be self-explanatory).

---

## TESTING STRATEGY

### The Test Pyramid
*   **Unit Tests (80%)**: Focus on individual functions. Must be fast (<1s) with no external I/O (use mocks for S3, JWT, etc.).
*   **Integration Tests (15%)**: Test component interactions (Router + Auth + Storage). Test Coverage > 90%.
*   **E2E Tests (5%)**: Full-stack workflows with real MinIO. Mark with `#[ignore]` for manual runs.

### Organization
```text
tests/
├── unit/           # Fast unit logic
├── integration/    # Component orchestration
└── e2e/            # Full proxy/system flows
```

## RESOURCES
* README.md: Quick start and overview.
* `docs/00_rule.md`: Project Rule.
* `docs/00_spec.md`: Full technical specification.
* `docs/00_plan.md`: The master TDD implementation checklist.
* `docs/00_agent_team.md`: RAGENT agent team and workflow.
* `docs/00_journal.md`: Team reflection that prevents the same mistake from happening again. Create blameless, actionable, and documented guidelines by **DOMAIN**.

