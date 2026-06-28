"""Pydantic I/O schemas for the `/skills/v1` router (T-SK).

A skill is a user-owned, reusable instruction preset. The owner (`user_id`) is
NEVER part of these schemas — it is resolved from the request (auth/middleware)
and supplied by the router, so a client cannot create or read a skill under a
different user by setting a body field.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Field bounds — kept in one place so request schemas and the DB column widths
# (migrations/013_skills.sql) stay in lockstep.
NAME_MAX = 128
DESCRIPTION_MAX = 512
INSTRUCTIONS_MAX = 16384


class SkillWriteRequest(BaseModel):
    """Body for POST (create) and PUT (full replace) — identical shape.

    PUT is a full replace (REST semantics): every field is re-sent. `enabled`
    defaults to True so a create/replace that omits it activates the skill.
    """

    name: str = Field(..., min_length=1, max_length=NAME_MAX)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    instructions: str = Field(..., min_length=1, max_length=INSTRUCTIONS_MAX)
    enabled: bool = True


class SkillResponse(BaseModel):
    skill_id: str
    name: str
    description: str
    instructions: str
    enabled: bool
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC


class SkillListResponse(BaseModel):
    skills: list[SkillResponse]
