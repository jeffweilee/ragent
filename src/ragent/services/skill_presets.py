"""Built-in preset skills (T-SK presets).

Presets are skills every user has from the start **without creating them** —
they live in code (not the DB), are read-only, and are merged into the
owner-scoped list/get/resolve paths by ``SkillService``. Adding another preset
later is a one-entry change here; no migration, no per-user seeding.

Design notes:
- A preset's ``skill_id`` is a stable, human-readable slug (e.g. ``skill-creator``)
  — distinct from the 26-char Crockford ids minted for user rows — so it can be
  referenced verbatim in ``forwardedProps.skillId`` and ``GET /skills/v1/{id}``.
- Presets are NOT stored per user, so updating a preset's instructions here
  changes it for everyone at once (the point of a built-in).
- ``name`` collisions are forbidden for user skills (``SkillService.create`` /
  ``update`` reject any name matching a preset, case-insensitively via
  ``PRESET_NAMES_CASEFOLD``) so the merged list is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

from ragent.schemas.skill import SkillResponse

# Fixed timestamps for built-ins — they have no real create/update event. A
# constant keeps the SkillResponse shape identical to user skills without
# implying a mutable history.
_PRESET_TS = "2026-01-01T00:00:00+00:00"

_SKILL_CREATOR_INSTRUCTIONS = (
    "You are Skill Creator, an assistant that helps the user design and save a "
    "reusable 'skill' — a named instruction preset they can apply to a future "
    "chat turn — and then saves it with the `create_skill` tool.\n\n"
    "A skill has three fields:\n"
    "- name: a short, unique label (<=128 chars).\n"
    "- description: one line on what the skill is for (<=512 chars).\n"
    "- instructions: the operating instructions the skill applies, written as a "
    "system prompt in the second person ('You are…', 'Always…', 'Never…'). When "
    "relevant, cover the role, the task, its scope and limits, the preferred "
    "output format/tone, and what to avoid (<=16384 chars).\n\n"
    "Default to DRAFTING, not interrogating. From whatever the user gives you — "
    "even a single sentence, or 'save what you just did' — propose a complete "
    "first draft of all three fields, inferring sensible defaults, and state any "
    "assumption you made in one short line so the user can correct it. Ask a "
    "clarifying question only when a field genuinely cannot be drafted; never ask "
    "for what you can reasonably infer.\n\n"
    "Show the draft and let the user edit it in plain language ('make it more "
    "formal', 'call it research-helper', 'always cite sources'). Iterate until "
    "the user confirms.\n\n"
    "On confirmation, call the `create_skill` tool to save the skill under the "
    "user's account. After it saves, tell the user the skill is stored and that "
    "they can select it for a future chat turn. You can only create new skills, "
    "not edit or delete existing ones; keep the name concise and don't invent "
    "requirements the user did not ask for."
)


@dataclass(frozen=True)
class PresetSkill:
    skill_id: str
    name: str
    description: str
    instructions: str
    enabled: bool = True

    def to_response(self) -> SkillResponse:
        return SkillResponse(
            skill_id=self.skill_id,
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            enabled=self.enabled,
            readonly=True,
            created_at=_PRESET_TS,
            updated_at=_PRESET_TS,
        )


# Add a new preset by appending an entry here — that is the entire change.
PRESETS: tuple[PresetSkill, ...] = (
    PresetSkill(
        skill_id="skill-creator",
        name="skill-creator",
        description="Helps you design and save a new reusable skill.",
        instructions=_SKILL_CREATOR_INSTRUCTIONS,
    ),
)

PRESET_BY_ID: dict[str, PresetSkill] = {p.skill_id: p for p in PRESETS}
# Case-folded for the reserved-name check: the DB's utf8mb4 collation makes user
# skill names case-insensitive, so the preset reservation matches that — a user
# can't create "Skill-Creator" to shadow the built-in "skill-creator".
PRESET_NAMES_CASEFOLD: frozenset[str] = frozenset(p.name.casefold() for p in PRESETS)
