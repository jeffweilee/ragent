"""MCP tool descriptor for `create_skill` (§3.8.3, T-SK).

A WRITE tool: it creates a skill under the **authenticated caller's** account
(the MCP router supplies the owner from `get_user_id`; `user_id` is never a tool
argument). The advertised inputSchema mirrors `schemas.skill.SkillWriteRequest`
bounds; the router validates arguments against that schema at call time.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations

from ragent.routers.mcp_tools.skill_tools import SKILL_BRIEF_SCHEMA, SKILL_WRITE_PROPERTIES

CREATE_SKILL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "instructions"],
    "properties": SKILL_WRITE_PROPERTIES,
}

CREATE_SKILL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skill"],
    "properties": {"skill": SKILL_BRIEF_SCHEMA},
}

CREATE_SKILL_TOOL = Tool(
    name="create_skill",
    description=(
        "Save a new reusable skill (instruction preset) under the current user's account. "
        "Use this once the user has confirmed the skill's name, description, and instructions. "
        "The skill is created for the authenticated user only; it cannot be created for anyone "
        "else. structuredContent.skill carries the created skill's id and name. On a name "
        "conflict the call fails — ask the user for a different name and retry."
    ),
    annotations=ToolAnnotations(readOnlyHint=False),
    inputSchema=CREATE_SKILL_INPUT_SCHEMA,
    outputSchema=CREATE_SKILL_OUTPUT_SCHEMA,
)
