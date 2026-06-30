# Skills — per-user reusable instruction presets (T-SK)

> Linked from [`docs/00_spec.md` §3.10](../00_spec.md#310-skills--per-user-reusable-instruction-presets-t-sk).

A **skill** is a user-owned, reusable instruction preset (a persona / system
instruction the user can attach to a `/chatagent/v3` turn). Skills are private:
the owner is the resolved `user_id` (auth/middleware), never a body field, and
**every** repository statement filters by `user_id`, so one user can never read
or mutate another's skills (isolation enforced at the SQL layer + the DB
`(user_id, name)` UNIQUE key, not by an application check alone).

**Built-in preset skills (T-SK):** some skills are **built in** — every user has
them from the start without creating them. Presets live in code
(`services/skill_presets.py`), not the DB, are **read-only**, and are merged into
the owner-scoped `list`/`get`/`resolve` paths (pinned ahead of the user's own
skills). The first preset is **`skill-creator`** (`skill_id="skill-creator"`),
whose instructions guide the agent to **draft** a complete skill (name /
description / instructions) from the user's intent — proposing a first version
rather than interrogating field by field — and, on confirmation, call the
`create_skill` MCP tool to save it. Adding more presets later =
one entry in the registry (no migration, no per-user seeding). Constraints: a user skill may not take a
preset's `name` (case-insensitive, matching the DB's utf8mb4 collation → `409
SKILL_NAME_CONFLICT`) — on `PUT` this is reported only **after** the target row
is confirmed owned, so a foreign/missing id stays `404` (foreign and missing are
indistinguishable); `PUT`/`DELETE` on a preset `skill_id` → `409 SKILL_READONLY`;
`resolve` of a preset returns its instructions (so
`forwardedProps.skillId="skill-creator"` works in `/chatagent/v3`).

**CRUD — `/skills/v1`** (always registered; no env gate):

- `POST /skills/v1` → `201` `{skill_id, name, description, instructions, enabled, readonly, created_at, updated_at}`.
  Body `{name, description?, instructions, enabled?}` (`enabled` defaults `true`).
  `readonly` is a server-derived response field (not a DB column): `true` for
  built-in presets, `false` for the user's own skills — so the frontend can
  flag built-ins without hard-coding preset ids.
- `GET /skills/v1` → `{ "skills": [ … ] }` (owner's skills, newest first; empty array, never `null`).
- `GET /skills/v1/{skill_id}` → the skill, or `404 SKILL_NOT_FOUND` when absent **or owned by another user** (a foreign skill is indistinguishable from a missing one — no existence oracle).
- `PUT /skills/v1/{skill_id}` → full replace (same body as POST) → the updated skill.
- `DELETE /skills/v1/{skill_id}` → `204`.
- Errors: `404 SKILL_NOT_FOUND` · `409 SKILL_NAME_CONFLICT` (duplicate `(user_id, name)`) · `422 SKILL_VALIDATION` (schema/field bounds) · `422 MISSING_USER_ID` (no resolved identity).

**Conversation flow — referencing a skill on a `/chatagent/v3` turn:**

```
client                         ragent /chatagent/v3                upstream ChatAgent
  │  RunAgentInput +                  │                                   │
  │  forwardedProps:{skillId}  ─────▶ │  resolve_instructions(user_id,    │
  │                                   │     skillId)  ── owner-scoped ──▶ skills(DB)
  │                                   │  append ContextItem(instructions) │
  │                                   │     to RunAgentInput.context       │
  │                                   │  ── existing caller wraps it in ──▶│ reads <hidden>
  │  ◀── twp-ai SSE (RUN_*) ──────────│     <hidden><context>…</context>   │   context, runs
```

- The skill_id rides in `forwardedProps.skillId` (the AG-UI extensibility field) — no change to the twp-ai `RunAgentInput` contract.
- The router resolves it **owner-scoped** and appends the instructions as a `ContextItem`, reusing the existing machine-context path: the upstream caller wraps `context` into the `<hidden><context>…</context></hidden>` block. This deliberately respects two existing invariants — (a) **upstream rule:** the upstream agent owns its own loop and reads the `<hidden>` block as machine-supplied context (we do not impose a structural persona), and (b) **memory storage:** the upstream persists every turn verbatim by `session=threadId`, and the v3 session-read (`strip_machine_context`) strips the `<hidden>` block, so the injected instructions never leak into the rendered/served history — identical treatment to client `context`/`state`. Instructions are re-sent each turn (keeping the persona active across a long conversation) but are stripped on read, so served memory stays clean.
- A missing / foreign / **disabled** skill is a hard error surfaced as a `RUN_ERROR` event (`code=SKILL_NOT_FOUND`) over the `200` stream — v3 never returns an HTTP 4xx — and the upstream is never called for that turn.

**Data structure** — table `skills` (migration `013_skills.sql`): surrogate `id BIGINT` PK; `skill_id CHAR(26)` UUIDv7→Crockford Base32 business key (UNIQUE); `user_id VARCHAR(64)`; `name VARCHAR(128)`; `description VARCHAR(512)`; `instructions MEDIUMTEXT` (not `TEXT` — the 16,384-char cap is 65,536 bytes under utf8mb4 worst case, one past `TEXT`'s 65,535-byte limit); `enabled BOOLEAN`; `created_at`/`updated_at DATETIME(6)`. `UNIQUE (user_id, name)`; index `(user_id, created_at, id)` backs the newest-first list without a filesort (point lookups use `uq_skill_id`). No physical FK (per §Database Practices).
