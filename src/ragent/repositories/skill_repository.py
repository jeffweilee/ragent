"""SkillRepository — owner-scoped CRUD for the `skills` table (T-SK).

Per 00_rule.md Database Practices each method checks out a fresh async
connection from the engine's pool and releases it on exit.

ISOLATION INVARIANT: every statement filters by `user_id`. There is no method
that reads or mutates a skill by `skill_id` alone — a caller can only ever
touch rows it owns, so cross-user access is impossible at the SQL layer (not
merely by an application-level check that a future refactor could drop).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ragent.utility.datetime import utcnow
from ragent.utility.id_gen import new_id

_INSERT_SQL = text(
    """
    INSERT INTO skills (
      skill_id, user_id, name, description, instructions, enabled,
      created_at, updated_at
    ) VALUES (
      :skill_id, :user_id, :name, :description, :instructions, :enabled,
      :created_at, :updated_at
    )
    """
)

_GET_SQL = text("SELECT * FROM skills WHERE user_id = :user_id AND skill_id = :skill_id")

_LIST_SQL = text("SELECT * FROM skills WHERE user_id = :user_id ORDER BY created_at DESC, id DESC")

_UPDATE_SQL = text(
    """
    UPDATE skills
       SET name = :name,
           description = :description,
           instructions = :instructions,
           enabled = :enabled,
           updated_at = :updated_at
     WHERE user_id = :user_id AND skill_id = :skill_id
    """
)

_DELETE_SQL = text("DELETE FROM skills WHERE user_id = :user_id AND skill_id = :skill_id")


class SkillRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def create(
        self,
        *,
        user_id: str,
        name: str,
        description: str,
        instructions: str,
        enabled: bool,
    ) -> str:
        """Insert a new skill and return its freshly minted ``skill_id``.

        A duplicate ``(user_id, name)`` raises the driver's ``IntegrityError``
        (the DB enforces per-owner name uniqueness); the service maps it to a
        409 conflict.
        """
        now = utcnow()
        skill_id = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                _INSERT_SQL,
                {
                    "skill_id": skill_id,
                    "user_id": user_id,
                    "name": name,
                    "description": description,
                    "instructions": instructions,
                    "enabled": enabled,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return skill_id

    async def get(self, *, user_id: str, skill_id: str) -> RowMapping | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(_GET_SQL, {"user_id": user_id, "skill_id": skill_id})
            return result.mappings().first()

    async def list(self, *, user_id: str) -> list[RowMapping]:
        async with self._engine.connect() as conn:
            result = await conn.execute(_LIST_SQL, {"user_id": user_id})
            return list(result.mappings().all())

    async def update(
        self,
        *,
        user_id: str,
        skill_id: str,
        name: str,
        description: str,
        instructions: str,
        enabled: bool,
    ) -> int:
        """Full-replace an owned skill. Returns rowcount (0 == not owned / absent)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                _UPDATE_SQL,
                {
                    "user_id": user_id,
                    "skill_id": skill_id,
                    "name": name,
                    "description": description,
                    "instructions": instructions,
                    "enabled": enabled,
                    "updated_at": utcnow(),
                },
            )
            return result.rowcount

    async def delete(self, *, user_id: str, skill_id: str) -> int:
        """Delete an owned skill. Returns rowcount (0 == not owned / absent)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(_DELETE_SQL, {"user_id": user_id, "skill_id": skill_id})
            return result.rowcount
