"""SystemSettingsRepository (T-EM.7) — generic key/JSON CRUD over `system_settings`.

Backed by migration 009. Single source of truth for runtime-mutable settings
that the App reads via TTL-cached `ActiveModelRegistry` (T-EM.9).

Async pool checkout per call (00_rule.md §Database Practices); no long-lived
connection. JSON serialization is application-side so MariaDB just stores
text — keeps driver compatibility simple.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import bindparam, text


def _decode(stored: Any) -> Any:
    """JSON-decode a value pulled from MariaDB.

    Different DBAPI drivers return JSON columns either as a string (`mysqlclient`,
    `pymysql`) or already-decoded (`asyncmy` with certain settings). Handle both.
    """
    if isinstance(stored, (str, bytes, bytearray)):
        return json.loads(stored)
    return stored


class SystemSettingsRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def get(self, key: str) -> Any:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("SELECT setting_value FROM system_settings WHERE setting_key = :key"),
                {"key": key},
            )
            row = result.mappings().first()
        if row is None:
            return None
        return _decode(row["setting_value"])

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Fetch multiple keys in one round-trip. Missing keys are omitted."""
        if not keys:
            return {}
        stmt = text(
            "SELECT setting_key, setting_value FROM system_settings WHERE setting_key IN :keys"
        ).bindparams(bindparam("keys", expanding=True))
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt, {"keys": list(keys)})
            rows = result.mappings().all()
        return {row["setting_key"]: _decode(row["setting_value"]) for row in rows}

    async def set(self, key: str, value: Any) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO system_settings (setting_key, setting_value)
                    VALUES (:key, CAST(:value AS JSON))
                    ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                    """
                ),
                {"key": key, "value": json.dumps(value)},
            )

    async def transition(
        self,
        updates: dict[str, Any],
        *,
        expect: dict[str, Any] | None = None,
    ) -> None:
        """Write multiple keys in a single transaction (lifecycle moves).

        When `expect` is provided, the same TX first runs `SELECT ... FOR
        UPDATE` against the named keys and compares the live values to the
        expected ones. A mismatch raises `OptimisticLockMismatch` and aborts
        the upserts — closes the concurrent-admin-action race window where
        two callers pass the service-layer state check on a stale TTL cache
        and both proceed to write.
        """
        async with self._engine.begin() as conn:
            if expect:
                expect_keys = list(expect.keys())
                live_rows = await conn.execute(
                    text(
                        "SELECT setting_key, setting_value FROM system_settings "
                        "WHERE setting_key IN :keys FOR UPDATE"
                    ).bindparams(bindparam("keys", expanding=True)),
                    {"keys": expect_keys},
                )
                live = {
                    row["setting_key"]: _decode(row["setting_value"])
                    for row in live_rows.mappings().all()
                }
                for k, want in expect.items():
                    if live.get(k) != want:
                        raise OptimisticLockMismatch(
                            f"settings.{k} changed since snapshot — "
                            f"expected {want!r}, found {live.get(k)!r}"
                        )
            for key, value in updates.items():
                await conn.execute(
                    text(
                        """
                        INSERT INTO system_settings (setting_key, setting_value)
                        VALUES (:key, :value)
                        ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                        """
                    ),
                    {"key": key, "value": json.dumps(value)},
                )


class OptimisticLockMismatch(Exception):
    """Raised by `transition(..., expect=...)` when a watched key drifted."""
