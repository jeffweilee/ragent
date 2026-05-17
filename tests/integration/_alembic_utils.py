"""Shared helpers for integration tests that need to apply alembic migrations
against a fresh testcontainer DB. Encapsulates the env-var swap dance so each
integration module does not reinvent it.
"""

from __future__ import annotations

import os
from pathlib import Path


def apply_alembic_head(sync_dsn: str) -> None:
    """Run ``alembic upgrade head`` against the given sync MariaDB DSN.

    Alembic's env.py reads ``MARIADB_DSN`` from the environment, so the
    helper swaps the env var for the duration of the upgrade and restores
    it afterwards.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parent.parent.parent / "alembic.ini"))
    old = os.environ.get("MARIADB_DSN")
    os.environ["MARIADB_DSN"] = sync_dsn
    try:
        command.upgrade(cfg, "head")
    finally:
        if old is None:
            os.environ.pop("MARIADB_DSN", None)
        else:
            os.environ["MARIADB_DSN"] = old
