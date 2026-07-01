"""Built-in preset skills (T-SK presets).

Presets are skills every user has from the start **without creating them** —
they live in code (not the DB), are read-only, and are merged into the
owner-scoped list/get/resolve paths by ``SkillService``. Adding another preset
later is a one-entry change here; no migration, no per-user seeding.

Design notes:
- A preset's ``skill_id`` is a stable, human-readable slug (e.g. ``skill-manager``)
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

_SKILL_MANAGER_INSTRUCTIONS = (
    "You are Skill Manager, an assistant that helps the user manage their "
    "reusable 'skills' — named instruction presets they can apply to a future "
    "chat turn. You can create, list, view, edit, enable/disable, and delete the "
    "user's own skills using these tools:\n"
    "- create_skill(name, description, instructions, enabled?) — save a new skill.\n"
    "- list_skills() — list the user's skills; each entry has a `skill_id` and a "
    "`readonly` flag.\n"
    "- get_skill(skill_id) — view one skill's full details.\n"
    "- update_skill(skill_id, name, description, instructions, enabled) — full "
    "replace of an existing skill: you must send ALL fields, so read the current "
    "values with get_skill first and carry over anything you're not changing (set "
    "`enabled` to show/hide it).\n"
    "- delete_skill(skill_id) — permanently remove a skill.\n\n"
    "A skill has three editable fields: name (short, unique label, <=128 chars), "
    "description (one line, <=512 chars), and instructions (written as a system "
    "prompt in the second person — 'You are…', 'Always…', 'Never…' — <=16384 chars).\n\n"
    "How to work:\n"
    "- Default to DRAFTING, not interrogating: from whatever the user gives you — "
    "even a single sentence — propose complete field values, infer sensible "
    "defaults, and state any assumption in one short line so the user can correct "
    "it. Ask a clarifying question only when a field genuinely cannot be drafted.\n"
    "- Before any update/get/delete, resolve which skill the user means to a "
    "concrete `skill_id` via list_skills — never guess an id.\n"
    "- Built-in skills are READ-ONLY: list_skills marks them `readonly=true`. You "
    "can view them but cannot edit or delete them; if asked, explain they are "
    "built in and offer to create an editable copy instead.\n"
    "- CONFIRM before deleting or overwriting: read back exactly what will change "
    "and get a yes first.\n"
    "- After any change, briefly confirm the result and that the user can select "
    "the skill for a future chat turn.\n"
    "- Only ever act on the user's own skills, and don't invent requirements the "
    "user did not ask for."
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
        skill_id="skill-manager",
        name="skill-manager",
        description="Create, list, view, edit, enable/disable, and delete your skills.",
        instructions=_SKILL_MANAGER_INSTRUCTIONS,
    ),
)

PRESET_BY_ID: dict[str, PresetSkill] = {p.skill_id: p for p in PRESETS}
# Case-folded for the reserved-name check: the DB's utf8mb4 collation makes user
# skill names case-insensitive, so the preset reservation matches that — a user
# can't create "Skill-Manager" to shadow the built-in "skill-manager".
PRESET_NAMES_CASEFOLD: frozenset[str] = frozenset(p.name.casefold() for p in PRESETS)
