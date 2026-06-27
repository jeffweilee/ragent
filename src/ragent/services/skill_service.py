"""SkillService — business logic for user-owned skill presets (T-SK).

Coordinates the owner-scoped ``SkillRepository`` and maps storage outcomes to
typed domain exceptions that the global handler renders as problem+json
(00_rule.md §API Error Honesty). Every public method emits an entry and an
exit (success / failure) structured log per 00_rule.md §Service Boundary Logs —
identity fields only (``user_id`` / ``skill_id``), never the instruction text.
"""

from __future__ import annotations

from typing import Any, NoReturn

import structlog
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from ragent.errors.codes import HttpErrorCode
from ragent.schemas.skill import SkillResponse
from ragent.services.skill_presets import PRESET_BY_ID, PRESET_NAMES_CASEFOLD, PRESETS
from ragent.utility.datetime import from_db, to_iso

logger = structlog.get_logger(__name__)


class SkillNotFoundError(Exception):
    """The requested skill_id is not owned by the caller (or does not exist)."""

    error_code = HttpErrorCode.SKILL_NOT_FOUND
    http_status = 404


class SkillNameConflictError(Exception):
    """A skill with this name already exists for this owner."""

    error_code = HttpErrorCode.SKILL_NAME_CONFLICT
    http_status = 409


class SkillReadOnlyError(Exception):
    """A built-in preset skill cannot be modified or deleted."""

    error_code = HttpErrorCode.SKILL_READONLY
    http_status = 409


def _to_response(row: RowMapping) -> SkillResponse:
    return SkillResponse(
        skill_id=row["skill_id"],
        name=row["name"],
        description=row["description"],
        instructions=row["instructions"],
        enabled=bool(row["enabled"]),
        created_at=to_iso(from_db(row["created_at"])),
        updated_at=to_iso(from_db(row["updated_at"])),
    )


class SkillService:
    def __init__(self, repository: Any) -> None:
        self._repo = repository

    async def create(
        self, *, user_id: str, name: str, description: str, instructions: str, enabled: bool
    ) -> SkillResponse:
        logger.info("skill.create", user_id=user_id)
        self._reject_preset_name(name, user_id)
        try:
            skill_id = await self._repo.create(
                user_id=user_id,
                name=name,
                description=description,
                instructions=instructions,
                enabled=enabled,
            )
        except IntegrityError:
            logger.warning(
                "skill.create.conflict",
                user_id=user_id,
                error_code=HttpErrorCode.SKILL_NAME_CONFLICT,
            )
            raise SkillNameConflictError(f"skill name already exists: {name!r}") from None
        logger.info("skill.created", user_id=user_id, skill_id=skill_id)
        return await self.get(user_id=user_id, skill_id=skill_id)

    @staticmethod
    def _not_found(user_id: str, skill_id: str) -> NoReturn:
        logger.info(
            "skill.not_found",
            user_id=user_id,
            skill_id=skill_id,
            error_code=HttpErrorCode.SKILL_NOT_FOUND,
        )
        raise SkillNotFoundError(f"skill not found: {skill_id}")

    @staticmethod
    def _reject_preset_name(name: str, user_id: str) -> None:
        """A user skill may not take a built-in preset's name (keeps the merged
        list unambiguous). Case-insensitive to match the DB's utf8mb4 collation —
        ``Skill-Creator`` cannot shadow the built-in ``skill-creator``."""
        if name.casefold() in PRESET_NAMES_CASEFOLD:
            logger.warning(
                "skill.create.conflict",
                user_id=user_id,
                error_code=HttpErrorCode.SKILL_NAME_CONFLICT,
            )
            raise SkillNameConflictError(f"name reserved by a built-in skill: {name!r}")

    @staticmethod
    def _reject_preset_mutation(skill_id: str, user_id: str) -> None:
        """Built-in presets live in code, not the user's collection — they cannot
        be updated or deleted."""
        if skill_id in PRESET_BY_ID:
            logger.warning(
                "skill.readonly",
                user_id=user_id,
                skill_id=skill_id,
                error_code=HttpErrorCode.SKILL_READONLY,
            )
            raise SkillReadOnlyError(f"built-in skill is read-only: {skill_id}")

    async def get(self, *, user_id: str, skill_id: str) -> SkillResponse:
        logger.info("skill.get", user_id=user_id, skill_id=skill_id)
        preset = PRESET_BY_ID.get(skill_id)
        if preset is not None:
            return preset.to_response()
        row = await self._repo.get(user_id=user_id, skill_id=skill_id)
        if row is None:
            self._not_found(user_id, skill_id)
        return _to_response(row)

    async def list_for_user(self, *, user_id: str) -> list[SkillResponse]:
        logger.info("skill.list", user_id=user_id)
        rows = await self._repo.list(user_id=user_id)
        logger.info("skill.listed", user_id=user_id, result_count=len(rows))
        # Built-in presets are pinned ahead of the user's own skills.
        return [p.to_response() for p in PRESETS] + [_to_response(r) for r in rows]

    async def update(
        self,
        *,
        user_id: str,
        skill_id: str,
        name: str,
        description: str,
        instructions: str,
        enabled: bool,
    ) -> SkillResponse:
        logger.info("skill.update", user_id=user_id, skill_id=skill_id)
        self._reject_preset_mutation(skill_id, user_id)
        # Confirm ownership BEFORE the reserved-name verdict: a foreign/missing id
        # must return 404 (foreign and missing are indistinguishable per contract),
        # not a 409 that leaks "this name is reserved".
        if await self._repo.get(user_id=user_id, skill_id=skill_id) is None:
            self._not_found(user_id, skill_id)
        self._reject_preset_name(name, user_id)
        try:
            rowcount = await self._repo.update(
                user_id=user_id,
                skill_id=skill_id,
                name=name,
                description=description,
                instructions=instructions,
                enabled=enabled,
            )
        except IntegrityError:
            logger.warning(
                "skill.update.conflict",
                user_id=user_id,
                skill_id=skill_id,
                error_code=HttpErrorCode.SKILL_NAME_CONFLICT,
            )
            raise SkillNameConflictError(f"skill name already exists: {name!r}") from None
        if rowcount == 0:  # deleted between the ownership check and the update (race)
            self._not_found(user_id, skill_id)
        logger.info("skill.updated", user_id=user_id, skill_id=skill_id)
        return await self.get(user_id=user_id, skill_id=skill_id)

    async def delete(self, *, user_id: str, skill_id: str) -> None:
        logger.info("skill.delete", user_id=user_id, skill_id=skill_id)
        self._reject_preset_mutation(skill_id, user_id)
        rowcount = await self._repo.delete(user_id=user_id, skill_id=skill_id)
        if rowcount == 0:
            self._not_found(user_id, skill_id)
        logger.info("skill.deleted", user_id=user_id, skill_id=skill_id)

    async def resolve_instructions(self, *, user_id: str, skill_id: str) -> str:
        """Return the instructions of an owned, enabled skill for chat injection.

        Raises ``SkillNotFoundError`` when the skill is absent, owned by another
        user, or disabled — a referenced-but-unavailable skill is a hard error
        the v3 router surfaces as a ``RUN_ERROR`` (never a silent no-op).
        """
        logger.info("skill.resolve", user_id=user_id, skill_id=skill_id)
        preset = PRESET_BY_ID.get(skill_id)
        if preset is not None:
            if not preset.enabled:
                raise SkillNotFoundError(f"skill not found or disabled: {skill_id}")
            logger.info("skill.resolved", user_id=user_id, skill_id=skill_id)
            return preset.instructions
        row = await self._repo.get(user_id=user_id, skill_id=skill_id)
        if row is None or not bool(row["enabled"]):
            logger.info(
                "skill.resolve.not_found",
                user_id=user_id,
                skill_id=skill_id,
                error_code=HttpErrorCode.SKILL_NOT_FOUND,
            )
            raise SkillNotFoundError(f"skill not found or disabled: {skill_id}")
        logger.info("skill.resolved", user_id=user_id, skill_id=skill_id)
        return row["instructions"]
