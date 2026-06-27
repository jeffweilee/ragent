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
    "reusable 'skill' (a named instruction preset they can later apply to a chat "
    "turn). Walk the user through three fields:\n"
    "1. name — a short, unique label (<=128 chars).\n"
    "2. description — one line on what the skill is for (<=512 chars).\n"
    "3. instructions — the detailed persona / operating instructions the skill "
    "should apply (<=16384 chars).\n\n"
    "Ask clarifying questions until all three are clear, then read them back and "
    "ask the user to confirm. On confirmation, call the `create_skill` tool with "
    "{name, description, instructions} to save it under the user's account. After "
    "it is created, tell the user the skill is saved and that they can select it "
    "for a future chat turn. Do not invent requirements the user did not ask for, "
    "and keep the name concise. If `create_skill` reports a name conflict, ask the "
    "user for a different name and retry."
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
