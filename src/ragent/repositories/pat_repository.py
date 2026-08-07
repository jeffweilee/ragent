"""PatRepository — one encrypted PAT per user (T-PAT).

Per 00_rule.md Database Practices each method checks out a fresh async
connection from the engine's pool and releases it on exit.

ONE nt ⇄ ONE PAT: `user_id` is UNIQUE (`uq_pat_user`), so `upsert` is a single
`INSERT … ON DUPLICATE KEY UPDATE` — re-authorization and every successful
refresh overwrite the row in place and reset `status` to `active`. The stored
`pat_cipher` is always the AES-256-GCM envelope, never the plaintext PAT.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ragent.utility.datetime import utcnow

# `status = 'active'` in the UPDATE branch reactivates a row previously flipped
# to 'invalid' (refresh 401) — a fresh authorization or successful refresh is a
# clean slate. created_at is only meaningful on first insert; the UPDATE branch
# leaves it untouched.
_UPSERT_SQL = text(
    """
    INSERT INTO pat (
        user_id, pat_cipher, status,
        authorized_at, authorization_expires_at, created_at, updated_at
    )
    VALUES (
        :user_id, :pat_cipher, 'active',
        :authorized_at, :authorization_expires_at, :created_at, :updated_at
    )
    ON DUPLICATE KEY UPDATE
        pat_cipher = :pat_cipher,
        status = 'active',
        authorized_at = :authorized_at,
        authorization_expires_at = :authorization_expires_at,
        updated_at = :updated_at
    """
)

# Refresh rotates an EXISTING authorization, so it is UPDATE-only — deliberately
# NOT the upsert above. `_do_refresh` runs concurrently with revoke and awaits
# between its DB write and its cache write, so an INSERT here would let a
# rotation that started before a revoke recreate the row it just deleted
# (rowcount 0 tells the caller the authorization is gone). Creating a row is
# authorize's job alone.
_ROTATE_SQL = text(
    """
    UPDATE pat
       SET pat_cipher = :pat_cipher,
           status = 'active',
           updated_at = :updated_at
     WHERE user_id = :user_id
    """
)

_GET_SQL = text("SELECT * FROM pat WHERE user_id = :user_id")

_MARK_INVALID_SQL = text(
    "UPDATE pat SET status = 'invalid', updated_at = :updated_at WHERE user_id = :user_id"
)


class PatRepository:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def upsert(
        self,
        *,
        user_id: str,
        pat_cipher: str,
        authorization_expires_at: date | None = None,
    ) -> None:
        """Insert or overwrite the caller's PAT, (re)setting status to active.

        Authorize-only, so `authorized_at` is stamped here: re-authorizing
        restarts the authorization window, and `created_at` (first insert only)
        cannot express that while `updated_at` is overwritten by every refresh.
        """
        now = utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                _UPSERT_SQL,
                {
                    "user_id": user_id,
                    "pat_cipher": pat_cipher,
                    "authorized_at": now,
                    "authorization_expires_at": authorization_expires_at,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    async def rotate(self, *, user_id: str, pat_cipher: str) -> int:
        """Overwrite an existing PAT after a successful refresh. Returns rowcount
        (0 == the authorization was revoked while the refresh was in flight)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                _ROTATE_SQL,
                {"user_id": user_id, "pat_cipher": pat_cipher, "updated_at": utcnow()},
            )
            return result.rowcount

    async def get(self, *, user_id: str) -> RowMapping | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(_GET_SQL, {"user_id": user_id})
            return result.mappings().first()

    async def mark_invalid(self, *, user_id: str) -> int:
        """Flip the caller's PAT to invalid. Returns rowcount (0 == absent)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                _MARK_INVALID_SQL, {"user_id": user_id, "updated_at": utcnow()}
            )
            return result.rowcount
